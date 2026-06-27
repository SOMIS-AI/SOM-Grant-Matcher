# Deployment Note — Grant Matcher: matching-quality improvements

**Deployed:** 2026-06-26 &middot; **Commit:** `2b11422` on `azure` &middot; **Repo:** SOMIS-AI/SOM-Grant-Matcher

Improves match quality based on the June 18 matching-spreadsheet review. The core
problem: matching keyed on individual keywords rather than the meaning of whole
phrases, and two single-word hits could outscore one precise multi-word concept.

## What changed

1. **Phrase-aware scoring** — a full concept phrase ("kidney cancer") now outscores
   two loose single-word hits ("kidney" + "cancer"). Previously the score summed
   `min(IDF, 5.0)` per keyword, which inverted this.
   _(`src/matcher.py`, config `matching.phrase_scoring`)_

2. **Single-component demotion** — faculty matching only an isolated component word
   of a grant's core concept (e.g. a polycystic-kidney-disease researcher hitting
   bare "kidney" on a kidney-**cancer** grant) are demoted, not top-ranked. Demoted
   but still visible in the diagnostic.

3. **Publication-based embeddings** — faculty embeddings now include PubMed
   `ArticleTitle` and NIH RePORTER `ProjectTitle` text, so researchers whose phrasing
   differs from the grant (e.g. "substance use in pregnancy" vs "substance use
   disorder") are caught on the semantic channel. Embedding version **5 → 6**.
   _(`src/faculty_scraper.py`, `src/embedder.py`)_

4. **Semantic distinctive-concept guard** — prevents the richer embeddings from
   re-introducing the "kidney disease &ne; kidney cancer" confusion: a *semantic-only*
   match on a cancer grant must show genuine cancer evidence (titles / self-reported
   keywords, not auto-attached MeSH terms) or it is demoted.
   _(`src/matcher.py`, config `matching.semantic_concept_guard`)_

## Required activation step

Set **`FORCE_SCRAPE=true`** in Azure Application Settings before/at the next
scheduled run. This:

- regenerates all faculty embeddings (v6), and
- re-scrapes faculty to populate publication titles (`evidence_titles`).

The first scrape is a full (~1,282-faculty) enrichment pass and runs for a few hours
— expected. It fires at the next scheduled run (`NOTIFY_HOUR`, default 6 AM ET), not
immediately. Phrase scoring, demotion, and the concept guard are active **immediately**
and do not need the re-scrape.

> **IMPORTANT — turn `FORCE_SCRAPE` back off after the first successful run.**
> `FORCE_SCRAPE` is read at process startup and cleared only in memory after the first
> successful run. The Azure env var persists, so **every app restart** (deploy, idle
> recycle, platform maintenance) re-triggers another full multi-hour re-scrape. Once
> you confirm the first scrape completed (diagnostic email shows a healthy faculty
> count and `evidence_titles` populated / embeddings at v6), set `FORCE_SCRAPE=false`
> or delete it. It is idempotent — no data harm — just wasteful if left on.

## What to watch on the first run

- **Diagnostic email** — new `semantic_concept_guarded` counter confirms the cancer
  guard's behavior across all grants. Sanity-check confidence histograms for
  unexpected shifts.
- **Restart health email** to `RESTART_RECIPIENTS` confirms the new build is live.

## Tuning (config-only, no code change)

- Guard too aggressive? Lower `matching.semantic_concept_guard.demote_multiplier`
  or trim its `oncology` marker list in `config/config.yaml`.
- Disable publication embeddings: env `FACULTY_EMBED_PUBS=0`.
- Phrase scoring: `matching.phrase_scoring.*` (set `enabled: false` to revert).
- Semantic recall floor: `matching.min_semantic_confidence` (42 default; 40 = more
  recall, 45 = less noise).

## Pre-deploy validation

Tested on real Grants.gov text + real PubMed data:

- **Katrina Mark** (substance use in pregnancy) now surfaces on the maternal
  MH/SUD grant (HRSA-26-102) — was previously missed.
- **Watnick / Woodward / Meier** (kidney disease, transplant) no longer false-match
  the DoW Kidney Cancer award; the real kidney-cancer match ranks top.
