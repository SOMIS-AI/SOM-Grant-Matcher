"""
Faculty Eval App — self-reported research keywords
==================================================
Imports research keywords collected from faculty via a separate campaign
("Faculty Eval App"). Spreadsheets dropped into a local folder get parsed
here, cleaned, deduped, and accumulated into `data/eval_app_keywords.json`.
That JSON is read at scrape time by `faculty_scraper` and the keywords are
merged into each matching faculty's profile (matched by email).

Input format (per spreadsheet row):
    Department | User Name | Email | Research Keywords (free-text)

The "Research Keywords" field is messy in the wild — encountered formats:
  - Clean comma list:    "trauma, whole blood"
  - Semicolon-separated: "emergency radiology; policy and practice; ..."
  - Run-together CamelCase: "CTLung cancer screenPulmonary nodules..."
  - Paragraph form:      "1) To optimize and apply focused ultrasound for ..."
  - Opt-out responses:   "research is not my area of expertise"
  - HTML entities:       "Parkinson&rsquo;s Disease"

`parse_keywords_field` handles all of these; `import_spreadsheet` processes
a single .xlsx and merges into the running JSON store.
"""

from __future__ import annotations

import html
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Storage ──────────────────────────────────────────────────────────────────
# seed_data/eval_app_keywords.json holds the accumulated, cleaned keywords from
# all spreadsheets processed so far, keyed by lowercased email. Tracked in git
# (unlike data/, which is gitignored and lives on Azure Files at runtime) so
# updates ship with the container image and prod picks them up automatically.
DEFAULT_STORE_PATH = Path("seed_data/eval_app_keywords.json")


# ── Opt-out detection ────────────────────────────────────────────────────────
# Faculty often respond with a sentence rather than a keyword list when they
# don't want to participate. These patterns catch the most common forms so we
# don't end up with "research is not my area of expertise" as a "keyword".
_OPTOUT_PATTERNS = [
    r"\bis not my area\b",
    r"\bnot (?:my|in my) (?:area|specialty|field|focus)\b",
    r"\bi (?:have been|am) helping\b",
    r"\bi don'?t (?:do|have|conduct)\b",
    r"\bno research\b",
    r"\bnot (?:currently )?(?:doing|conducting|involved)\b",
    r"\bn/?a\s*$",
]
_OPTOUT_RE = re.compile("|".join(_OPTOUT_PATTERNS), re.I)


# ── Cleanup helpers ──────────────────────────────────────────────────────────
# Verb-like / sentence-fragment indicators. A "keyword" that contains any of
# these as a standalone word is almost certainly prose, not a research topic.
_SENTENCE_INDICATORS = {
    "is", "are", "was", "were", "am", "be", "been", "being", "have", "has", "had",
    "i", "we", "my", "our", "the", "a", "an", "this", "that", "these", "those",
    "with", "for", "to", "of", "in", "on", "at", "by", "from", "as",
    "would", "could", "should", "will", "do", "does", "did", "doing", "done",
    "interested", "interest", "interests", "research", "primary", "primarily",
}

# Phrases too short or too long to be a useful keyword
_MIN_KW_LEN = 3
_MAX_KW_LEN = 80


def _decode_text(raw: str) -> str:
    """Unescape HTML entities, normalize whitespace, strip outer quotes."""
    if not raw:
        return ""
    s = html.unescape(str(raw))
    s = re.sub(r"\s+", " ", s).strip()
    # Strip wrapping quotes that some users add
    s = s.strip('"\'')
    return s


def _is_optout(text: str) -> bool:
    """Detect opt-out / non-research responses."""
    return bool(_OPTOUT_RE.search(text or ""))


def _looks_like_sentence(s: str) -> bool:
    """Heuristic: does this 'keyword' look like a sentence fragment?
    Triggers on too many filler words or first-person pronouns."""
    tokens = [t.lower() for t in re.split(r"\s+", s) if t]
    if not tokens:
        return True
    filler = sum(1 for t in tokens if t in _SENTENCE_INDICATORS)
    # If half the tokens are filler / first-person, it's prose
    if filler >= max(2, len(tokens) // 2):
        return True
    # Also catches starts like "I am", "I have", "We are"
    if tokens[0] in {"i", "we", "my", "our"}:
        return True
    return False


def _split_camelcase_runs(text: str) -> str:
    """Insert a separator before any uppercase letter that starts a new word
    after the end of a previous one. Two patterns:
      1. lowercase|digit → Upper:  "SafetyQuality"     → "Safety|Quality"
      2. Upper Upper lower:        "CTLung cancer"     → "CT|Lung cancer"
                                   (the second-Upper starts a new word b/c
                                    it's followed by lowercase, so the first
                                    Upper closes an acronym)
    Acronyms like "PET/CT", "ECMO", "AAA" are preserved (no lowercase follows).
    """
    if not text:
        return text
    # Pattern 2 first — must come before pattern 1 so "CTLung" yields "CT|Lung",
    # not "CTLun|g" (no such case, but safer ordering).
    s = re.sub(r"([A-Z])([A-Z][a-z])", r"\1|\2", text)
    # Pattern 1
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1|\2", s)
    # Bonus pattern: ")Upper" — close-paren followed by capital letter is
    # almost always a missing delimiter ("(MOUD)Telemedicine" → "(MOUD)|Telemedicine").
    s = re.sub(r"(\))([A-Z])", r"\1|\2", s)
    return s


_PARAGRAPH_INDICATORS = re.compile(
    r"^\s*[1-9][\.\)]\s|"   # numbered list "1) ..." / "1. ..."
    r"\bi\s+(?:am|have|will|would|conduct|study|examine|research|focus)\b|"
    r"\bmy\s+research\b|"
    r"\bour\s+(?:research|lab|group|work)\b|"
    r"\bwe\s+(?:study|examine|conduct|focus|investigate)\b",
    re.I,
)


def _looks_like_paragraph(text: str) -> bool:
    """Heuristic: is this a prose paragraph rather than a keyword list? Used
    to decide whether to run the existing phrase extractor instead of
    splitting on delimiters (which mangles prose into nonsense chunks)."""
    if not text or len(text) < 120:
        return False
    return bool(_PARAGRAPH_INDICATORS.search(text))


def _mask_parens(text: str) -> tuple[str, dict]:
    """Replace each `(...)` group with a placeholder so subsequent splits
    (especially on commas) don't break content inside parens. Returns the
    masked text + a map from placeholder → original content. Use `_unmask`
    to restore."""
    masked = []
    spans = {}
    i = 0
    last = 0
    depth = 0
    start = -1
    for idx, ch in enumerate(text):
        if ch == "(":
            if depth == 0:
                masked.append(text[last:idx])
                start = idx
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
            if depth == 0:
                key = f"§{i}§"
                spans[key] = text[start:idx + 1]
                masked.append(key)
                i += 1
                last = idx + 1
    masked.append(text[last:])
    return "".join(masked), spans


def _unmask(text: str, spans: dict) -> str:
    for k, v in spans.items():
        text = text.replace(k, v)
    return text


def _split_candidates(text: str) -> list[str]:
    """Split a 'Research Keywords' value into candidate keyword strings.

    Strategy:
      1. Mask `(...)` groups so internal commas don't break paren content
      2. Normalize CamelCase runs into pipe-separated boundaries
      3. Strip numeric/bullet markers
      4. Split on pipe | semicolon | comma | newline
      5. Unmask parens in each piece

    Note: this does NOT yet validate the pieces — `_clean_one` does that.
    """
    if not text:
        return []
    # Apply the ")Upper" rule on the RAW text first so closing parens at real
    # word boundaries become explicit delimiters before mask_parens hides them.
    # ("(MOUD)Telemedicine" → "(MOUD)|Telemedicine")
    text = re.sub(r"(\))([A-Z])", r"\1|\2", text)
    masked, spans = _mask_parens(text)
    # 1. Disambiguate run-together CamelCase
    s = _split_camelcase_runs(masked)
    # 2. Strip leading numeric/bullet markers like "1) " "2. " "• " "- "
    s = re.sub(r"(?<=\s)\d+[\.\)]\s+", "|", s)
    s = re.sub(r"^\s*\d+[\.\)]\s+", "", s)
    s = re.sub(r"[•·▪◦●]\s*", "|", s)
    # 3. Split on the explicit delimiters
    pieces = re.split(r"[|;,\n\r]+", s)
    out = []
    for p in pieces:
        if not p:
            continue
        # Restore any parens content
        p = _unmask(p, spans)
        p = p.strip(" \t-•")
        if p:
            out.append(p)
    return out


def _clean_one(candidate: str) -> Optional[str]:
    """Validate + normalize a single candidate keyword. Returns the cleaned
    keyword or None to drop it.

    Rules:
      - Length must be _MIN_KW_LEN..MAX_KW_LEN chars
      - Must not look like a sentence (filler-word heuristic)
      - Strip surrounding punctuation
      - Collapse internal whitespace
      - Lowercase the FIRST character only if all-caps (preserve "PET/CT", "ECMO")
    """
    if not candidate:
        return None
    # NOTE: parens are intentionally NOT in this strip set — a phrase like
    # "Cognitive Functioning and Aging (normal aging, MCI, ...)" should keep
    # the trailing `)` since the inner content survived paren-aware splitting.
    s = candidate.strip(" \t\r\n-:.;,[]\"'")
    s = re.sub(r"\s+", " ", s)
    if len(s) < _MIN_KW_LEN or len(s) > _MAX_KW_LEN:
        return None
    if _looks_like_sentence(s):
        return None
    # Drop pure-number tokens, year-only tokens, etc.
    if re.fullmatch(r"[\d\W]+", s):
        return None
    return s


def parse_keywords_field(raw_value: str) -> list[str]:
    """Extract a clean, deduplicated list of keywords from one faculty's
    'Research Keywords' free-text response.

    Returns [] if the response is an opt-out, empty, or yields nothing usable.
    """
    text = _decode_text(raw_value)
    if not text or _is_optout(text):
        return []

    # Long prose paragraphs ("My research focuses on X, where we...") are not
    # safely split on delimiters — they need the phrase extractor used by the
    # rest of the system. Imported lazily to avoid a circular import.
    if _looks_like_paragraph(text):
        try:
            # When invoked via `python -m src.eval_app_keywords` from the repo
            # root, the sibling module isn't on sys.path. Add src/ as a fallback.
            if str(Path(__file__).parent) not in sys.path:
                sys.path.insert(0, str(Path(__file__).parent))
            from faculty_scraper import _extract_phrases_from_text
            phrases = _extract_phrases_from_text(text, max_phrases=20)
            if phrases:
                return phrases  # phrase extractor already dedupes + ranks
        except Exception as e:
            logger.warning(f"Phrase extractor unavailable, falling back: {e}")

    candidates = _split_candidates(text)

    # Fallback: if delimiter splitting produced one long lump (no commas or
    # CamelCase), still try the phrase extractor before giving up.
    if len(candidates) == 1 and len(candidates[0]) > 120:
        try:
            # When invoked via `python -m src.eval_app_keywords` from the repo
            # root, the sibling module isn't on sys.path. Add src/ as a fallback.
            if str(Path(__file__).parent) not in sys.path:
                sys.path.insert(0, str(Path(__file__).parent))
            from faculty_scraper import _extract_phrases_from_text
            phrases = _extract_phrases_from_text(candidates[0], max_phrases=20)
            if phrases:
                candidates = phrases
        except Exception as e:
            logger.warning(f"Phrase extractor unavailable, falling back: {e}")

    out: list[str] = []
    seen_lower: set[str] = set()
    for c in candidates:
        cleaned = _clean_one(c)
        if cleaned is None:
            continue
        k = cleaned.lower()
        if k in seen_lower:
            continue
        seen_lower.add(k)
        out.append(cleaned)
    return out


# ── Store I/O ────────────────────────────────────────────────────────────────

def load_store(path: Path = DEFAULT_STORE_PATH) -> dict:
    """Load the running eval-app-keywords store from disk. Returns the empty
    skeleton if the file doesn't exist or is unreadable."""
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "by_email" in data:
                return data
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not read eval-app keywords store: {e}")
    return {"updated_at": None, "sources": [], "by_email": {}}


def save_store(store: dict, path: Path = DEFAULT_STORE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    store["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


# ── Spreadsheet import ───────────────────────────────────────────────────────

def import_spreadsheet(xlsx_path: Path, store: dict) -> dict:
    """Read one .xlsx, parse every row, and MERGE its entries into the running
    store. Returns a stats dict {rows_total, with_keywords, optout, added,
    updated, unchanged}. Last-write-wins per email when re-uploading.
    """
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb.worksheets[0]  # "Export" sheet
    stats = {"rows_total": 0, "with_keywords": 0, "optout": 0,
             "added": 0, "updated": 0, "unchanged": 0, "skipped_malformed": 0}

    by_email = store.setdefault("by_email", {})

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        wb.close()
        return stats

    # Validate header
    header = [str(c or "").strip().lower() for c in rows[0]]
    expected = {"department", "user name", "email", "research keywords"}
    if not expected.issubset(set(header)):
        wb.close()
        raise ValueError(
            f"Unexpected header in {xlsx_path.name}: {header}. "
            f"Expected columns: {sorted(expected)}"
        )
    col_idx = {h: i for i, h in enumerate(header)}

    # openpyxl in read_only mode yields RAGGED rows: trailing empty cells are
    # omitted, so a row can be shorter than the header. The Faculty Eval App
    # export ends with a blank row and a one-cell "Applied filters: …" footer,
    # which arrive as 0- and 1-length tuples. Indexing those raised IndexError
    # partway through the loop — and because the CLI caught the exception and
    # saved anyway, the 04Sept2026 import wrote 846 merged records with NO entry
    # in `sources`: the data was there but its provenance was not (2026-09-04).
    # Pad short rows to the header width and count what gets skipped, so a
    # genuinely malformed export is visible rather than silently truncating the
    # import at the first bad row.
    width = len(header)
    for raw_row in rows[1:]:
        row = tuple(raw_row) + (None,) * (width - len(raw_row))
        # A row with no email at all is footer/blank, not data.
        if not any(str(c or "").strip() for c in row):
            stats["skipped_malformed"] += 1
            continue
        if len(raw_row) < width:
            stats["skipped_malformed"] += 1
            continue
        stats["rows_total"] += 1
        dept  = str(row[col_idx["department"]] or "").strip()
        name  = str(row[col_idx["user name"]] or "").strip()
        email = str(row[col_idx["email"]] or "").strip().lower()
        kw_raw = row[col_idx["research keywords"]]

        if not email or "@" not in email:
            continue

        keywords = parse_keywords_field(kw_raw)
        if not keywords:
            if kw_raw and _is_optout(_decode_text(kw_raw)):
                stats["optout"] += 1
            continue
        stats["with_keywords"] += 1

        existing = by_email.get(email)
        new_record = {
            "name":        name or (existing or {}).get("name", ""),
            "department":  dept or (existing or {}).get("department", ""),
            "keywords":    keywords,
            "source_file": xlsx_path.name,
            "updated_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if existing is None:
            stats["added"] += 1
        elif existing.get("keywords") != keywords:
            stats["updated"] += 1
        else:
            stats["unchanged"] += 1
        by_email[email] = new_record

    store.setdefault("sources", []).append({
        "filename":    xlsx_path.name,
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats":       stats,
    })
    wb.close()
    return stats


# ── Merge into faculty profiles (called from faculty_scraper) ────────────────

def get_keywords_by_email(path: Path = DEFAULT_STORE_PATH) -> dict[str, list[str]]:
    """Compact accessor used by the scrape pipeline: returns just
    {email_lower: [keywords]} so the merge step doesn't need to know the
    store's wider schema."""
    store = load_store(path)
    return {
        email: rec.get("keywords", [])
        for email, rec in store.get("by_email", {}).items()
        if rec.get("keywords")
    }


# ── Email backfill ───────────────────────────────────────────────────────────
# Not every UMSOM profile page publishes an email. Verified 2026-09-04 by
# fetching the pages directly: for the affected faculty there is no address in
# any form — no mailto, no obfuscation, no alternate domain — so the scraper's
# regex has nothing to find and no pattern change would help. Across the match
# archive that left 356 faculty and 1,637 delivered matches with a blank email,
# which makes their feedback unattributable AND excludes them from personalised
# digests entirely (that fan-out indexes by email).
#
# The Eval App export does carry an Email column, so a third of the gap can be
# closed from data already in the repo. Matching is on a normalised name and
# ONLY when it resolves to exactly one address — two real cases
# ("Sarah E. Woodson Smith", "Brian W. Jackson") map to two addresses each, and
# guessing between them would attach a verdict to the wrong person.

def normalize_person_name(name: str) -> str:
    """First + last, lowercased, credentials and middle initials stripped.
    Deliberately loose enough to match "Kristyn N. Donohue" against
    "Kristyn Donohue", and strict enough that collisions stay rare — when one
    does occur, resolve_emails_by_name() refuses to guess."""
    n = (name or "").lower()
    n = re.sub(r"\b(m\.?d\.?|ph\.?d\.?|d\.?o\.?|m\.?p\.?h\.?|r\.?n\.?|m\.?s\.?|"
               r"m\.?b\.?a\.?|d\.?d\.?s\.?|pharm\.?d\.?)\b", " ", n)
    n = re.sub(r"[^a-z ]", " ", n)
    parts = [p for p in n.split() if len(p) > 1]   # drops middle initials
    if len(parts) < 2:
        return " ".join(parts)
    return f"{parts[0]} {parts[-1]}"


def resolve_emails_by_name(path: Path = DEFAULT_STORE_PATH) -> dict[str, str]:
    """{normalised name: email} for names that map to exactly ONE address.
    Ambiguous names are omitted rather than arbitrarily resolved."""
    store = load_store(path)
    by_name: dict[str, set] = {}
    for email, rec in store.get("by_email", {}).items():
        key = normalize_person_name(rec.get("name", ""))
        if key and email:
            by_name.setdefault(key, set()).add(email)
    return {k: next(iter(v)) for k, v in by_name.items() if len(v) == 1}


# ── CLI entry point ──────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    """python -m src.eval_app_keywords <xlsx_path> [<xlsx_path>...]

    Reads each spreadsheet and merges into data/eval_app_keywords.json.
    Run this locally after dropping a new spreadsheet into the campaign folder
    — commit the resulting JSON and the keywords flow into prod on the next
    scheduled re-scrape (or sooner with FORCE_SCRAPE=true)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(argv) < 2:
        print("Usage: python -m src.eval_app_keywords <xlsx> [<xlsx>...]")
        return 2

    store = load_store()
    for arg in argv[1:]:
        p = Path(arg)
        if not p.exists():
            print(f"  ✗ Missing: {p}")
            continue
        print(f"  Importing {p.name}...")
        try:
            s = import_spreadsheet(p, store)
        except Exception as e:
            # import_spreadsheet merges INTO `store` as it goes, so a mid-loop
            # exception leaves it partially updated with no `sources` entry.
            # Saving that would persist records whose provenance is unrecorded —
            # which is exactly what happened on 2026-09-04. Abort instead and
            # leave the on-disk store untouched.
            print(f"  ✗ {p.name}: {type(e).__name__}: {e}")
            print("  ABORTED — the store on disk was NOT modified. "
                  "Fix the spreadsheet or the importer and re-run.")
            return 1
        print(f"    rows={s['rows_total']} usable={s['with_keywords']} "
              f"optout={s['optout']} added={s['added']} updated={s['updated']} "
              f"unchanged={s['unchanged']}"
              + (f" skipped_malformed={s['skipped_malformed']}"
                 if s.get("skipped_malformed") else ""))

    save_store(store)
    total = len(store.get("by_email", {}))
    print(f"\n[OK] Store now has {total} faculty with self-reported keywords")
    print(f"  -> {DEFAULT_STORE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv))
