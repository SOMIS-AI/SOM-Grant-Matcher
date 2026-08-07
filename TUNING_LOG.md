# Tuning Log

Every deliberate change to **matching behaviour** — thresholds, filters, scoring,
vocabulary, sources — with why it was tried, what it was meant to improve, and
whether it actually worked.

The point is not ceremony. It is to stop us re-trying something that already
failed, and to stop us assuming something worked because it sounded reasonable.

**Scope.** Anything that changes which grants reach which faculty. Infrastructure,
UI, deploy and email-formatting changes belong in the commit history, not here.

---

## How to add an entry

Add to the top of [The log](#the-log). Copy this block:

```markdown
### YYYY-MM-DD — Short name of the change
**Status:** proposed | live | reverted | superseded
**Commit:** `abcdef1`
**Change:** what actually changed, concretely (param X 45 -> 50, filter Y added).
**Why:** the observation that prompted it. Link the evidence — a diagnostic date,
a reviewer comment, a specific grant that was missed or wrongly matched.
**Expected effect:** the metric that should move, and in which direction.
**Outcome:** *(fill in once there is evidence — see Measuring an outcome)*
**Verdict:** worked | no effect | made it worse | not measured | too early
```

Two rules that make the log worth keeping:

1. **Write the entry when you make the change**, with `Outcome` blank. An entry
   written afterwards records what you remember, not what happened.
2. **Change one thing at a time** where you can. Most of the backfilled entries
   below are marked *not measured* precisely because several things moved at
   once and the effect can no longer be separated out.

## Measuring an outcome

Every run writes `grant_matcher_diagnostic_YYYY-MM-DD.json`, which records the
tuning parameters in force **and** the resulting counts. That archive is the
evidence base:

```bash
python tools/diag_trend.py "<Diag Files folder>"                  # full table
python tools/diag_trend.py "<Diag Files folder>" --params         # when knobs moved
python tools/diag_trend.py "<Diag Files folder>" --since 2026-08-01
python tools/diag_trend.py "<Diag Files folder>" --csv > trend.csv
```

`>>` marks a date where a parameter changed — compare the rows either side.

Watch `keep%` (delivered ÷ candidate matches) for threshold changes, `skipped`
for relevance-filter changes, and `sem` for anything touching the semantic
channel. Daily grant volume is small and lumpy (0–40), so give a change **at
least a week** before calling it, and prefer comparing weekly totals.

**A caution the archive itself teaches:** faculty pool size changed several times
(3,090 → 1,821 → 1,280) for unrelated reasons. Raw match counts are not
comparable across those boundaries; ratios like `keep%` are.

---

## The log

### 2026-08-07 — Relevance filter false negatives + Grants.gov detail fetch
**Status:** live
**Commit:** `43b051b`
**Change:** three relevance fixes — split the biomedical vocabulary into exact
and prefix-stem tiers; restrict common-English block terms (`education`, `labor`,
`commerce`, `interior`, `arts`, `humanities`) to the agency field instead of also
matching titles; add IHS to the agency allow-list. Added health-services
vocabulary. Separately, `enrich_grants_with_details()` now calls Grants.gov
`fetchOpportunity` for each new grant.
**Why:** the 08-06/08-07 diagnostic review. 18 of 24 grants on 08-07 were skipped
as irrelevant, including two IHS health-research programs dropped purely for
carrying "Education" in the title — on a day when other IHS grants matched
normally. Investigating that surfaced two deeper faults: the vocabulary regex
was `\b(...)\b`, so every truncated stem (`oncol`, `epidemiolog`, `hepat`,
`patholog`) had been **dead since it was written**; and `search2` returns no
synopsis at all, so every Grants.gov grant had been matched on its **title
alone**.
**Expected effect:** `skipped` falls. `raw` rises, possibly a lot, since semantic
matching finally has real text. Deadlines populate in emails and Excel.
**Outcome:** *pending — first run under this code is 2026-08-08.*
**Verdict:** too early
**Validation done before merge:** replayed all 406 historically skipped-irrelevant
grants with their recorded agencies; 19 now pass (Army rare-cancer research, HRSA
nursing, IHS health programs) with no wrongly-admitted grants after qualifying
`screening` and adding plant/veterinary guards.

### 2026-08-06 — Confidence floor 45 → 50
**Status:** live
**Commit:** `686522a` (config), effective in diagnostics from 08-06
**Change:** `min_confidence_score` and `min_semantic_confidence` 45/42 → 50.
**Why:** 65% of delivered rows sat at 45–49 — the floor was admitting a large
band of marginal matches right at the threshold, ahead of the faculty pilot.
**Expected effect:** fewer delivered matches, higher average quality.
**Outcome:** 08-04 (floor 45) kept 16.2% of candidates; 08-07 (floor 50) kept
13.4%. Directionally right but **two data points either side of a weekend, on
7–24 grants/day — not conclusive.**
**Verdict:** not measured — revisit once a fortnight of post-08-08 data exists.

### 2026-08-04 — Pilot readiness bundle
**Status:** live
**Commit:** `686522a`
**Change:** NOFO series clustering (one card per near-identical series);
`semantic_evidence()` fills the previously blank Matched Keywords cell on
semantic matches with `≈ term`; clinical-vs-basic filter switched from
report-only to live after 6 weeks' validation; three dead scrapers disabled
(alexs_lemonade, alzheimers_assoc, proposal_central — 77 consecutive empty runs);
stop-word `white` added (the "Ryan White" proper-noun leak seen 08-01).
**Why:** Jul-26..Aug-04 diagnostic review plus June reviewer feedback. The CDC
global-health-security announcements posted one-per-country were flooding inboxes
with near-duplicates.
**Expected effect:** fewer near-duplicate cards; semantic matches become
explainable rather than showing an empty evidence cell.
**Outcome:** series clustering validated pre-merge on the 9 CDC country twins vs
4 distinct controls. Inbox effect not separately measured.
**Verdict:** not measured (several changes shipped together)

### 2026-07-21 — IDF floor actually enforced
**Status:** live
**Commit:** `3b93f79`
**Change:** `min_idf_for_match >= 1.5` enforced in `_keyword_matches_for_grant`.
**Why:** the floor had been advertised in config and printed in diagnostics since
the June tuning but was **never applied** — `idf_filtered_keywords` was always
empty, and low-IDF roster-wide terms could still anchor a match.
**Expected effect:** fewer matches anchored on generic terms.
**Outcome:** not isolated — shipped with five other fixes.
**Verdict:** not measured
**Note:** a good argument for this log. A documented parameter sat inert for
roughly a month while diagnostics implied it was working.

### 2026-06-26 — Phrase-aware scoring + publication embeddings
**Status:** live
**Commit:** `2b11422`
**Change:** weight a matched keyword by meaningful-token count with a raised IDF
cap for multi-word phrases, so "kidney cancer" outscores "kidney" + "cancer"
(previously inverted); demote single-component matches; add
`min_semantic_confidence` so semantic matches clearing the threshold aren't killed
by the keyword-calibrated floor; fold PubMed/RePORTER titles into faculty
embeddings (`EMBEDDING_VERSION` 5 → 6).
**Why:** June 18 reviewer spreadsheet — matching keyed on individual keywords
rather than whole-phrase meaning, and two single-word hits outscored one precise
phrase.
**Expected effect:** precise phrase matches outrank incidental word overlap;
sparse-profile faculty become reachable semantically.
**Outcome:** one measured case recorded at the time — K. Mark rose 0.32 → 0.49
against the MMHSUD grant. No aggregate measurement.
**Verdict:** not measured

### 2026-06-23 — Track-record gate + context-keyword filter
**Status:** live
**Commits:** `7a8bd2c`, `bf504ed`
**Change:** drop a keyword match when *every* matched term is a generic
population/method word (`pediatric`, `childhood`, `risk`, …); weight matches by
external footprint (RePORTER ×1.05, publications ×1.00, self-reported-only ×0.80);
hard-gate no-footprint faculty off major mechanisms (R01/R35/U01/U54/UM1/P01/
P30/P50, DoD IIRA etc.), later extended to require nih-tier for K12/KL2/T32/T35.
**Why:** 2026-06-23 match-comment review, Themes 2 and 3 — e.g. faculty matching
a pediatric-cancer genome grant on "pediatric" + "childhood" alone.
**Expected effect:** `context_filtered` and `track_record_gated` become non-zero;
fewer implausible matches on flagship mechanisms.
**Outcome:** both counters have read **0 on every run in the 08-05..08-07
sample**. Either the conditions genuinely never fire at current volume, or the
gates are not engaging. **Unverified — worth checking.**
**Verdict:** not measured — see Open questions

### 2026-06-17 — Relevance filter reordered; DoD sub-commands allow-listed
**Status:** live
**Commit:** `db1c996`
**Change:** agency allow-list now runs *before* the block-list; added Office of
Naval Research, Army/Walter Reed, AFRL, Defense Health Agency and similar.
**Why:** SAMHSA's "Preventing Youth Overdose: Treatment, Recovery, Education,
Awareness" was rejected by the `education` block term despite SAMHSA being
allow-listed. The ONR "Special Program Announcement" miss surfaced in the same
audit — the DoD umbrella terms don't substring into "Office of Naval Research".
**Expected effect:** allow-listed agencies stop being blocked by incidental title
words.
**Outcome:** confirmed — both named grants pass under the current filter.
**Verdict:** worked (partially — the same class of bug survived for
*non*-allow-listed agencies until 2026-08-07; see the top entry)

### 2026-06-16 — Word-boundary the filter lists
**Status:** live
**Commit:** `ee04180`
**Change:** block/allow/non-bio lists compiled to `\b`-anchored regexes instead
of plain substring checks.
**Why:** `epa` matched "DEPArtment of Housing", so every HUD/DHS grant was
rejected with `blocked agency/title term: 'epa'`. Caught by the then-new
skipped-grants audit.
**Expected effect:** short acronym terms stop misfiring.
**Outcome:** confirmed by inspection; the reason string no longer appears.
**Verdict:** worked

### 2026-06-15 — Log skipped grants in the diagnostic
**Status:** live
**Commit:** `b5e06d4`
**Change:** record every filtered/skipped grant with its reason.
**Why:** filter decisions were invisible; we could see what matched, never what
was thrown away.
**Outcome:** this single change is responsible for finding the `epa` substring
bug (06-16), the SAMHSA/ONR misses (06-17), and the whole 2026-08-07 batch.
**Verdict:** worked — highest-leverage change in the log

### 2026-05-31 / 05-28 — Confidence floor and per-grant cap raised
**Status:** live
**Commits:** `74c4329` and config
**Change:** `min_confidence` 35 → 45 (05-28); `max_matches_per_grant` 20 → 30
(05-28) → 40 (05-31). Separately, removed the ×0.85 semantic-only confidence
penalty.
**Why:** the ×0.85 penalty interacted badly with the floor — semantic-only
matches needed similarity ≥ 0.53 when observed maxima were 0.45–0.51, so
semantic-only matches **collapsed from ~100/run to 1/run on 05-28**, effectively
switching off semantic discovery.
**Expected effect:** restore the semantic channel while the raised floor holds
quality.
**Outcome:** semantic-only recovered — `sem` has run in the 1–36/day range since.
**Verdict:** worked
**Don't repeat:** stacking a multiplier *and* an absolute floor on the same score.
The floor is calibrated for keyword confidence; applying it to penalised semantic
scores silently closed the channel. This is what `min_semantic_confidence`
(2026-06-26) now exists to prevent.

### 2026-05-24 — Hybrid "both" matching fixed; per-grant cap tightened
**Status:** live
**Commit:** `3c4f387`
**Change:** compute cosine similarity for *all* faculty once per grant, so
keyword matches clearing the semantic threshold upgrade to `both` with the ×1.15
agreement boost. `max_matches_per_grant` 50 → 20. Non-biomedical title phrases
added.
**Why:** the semantic pass excluded keyword-matched faculty, so `both` was
**structurally always 0** — the agreement boost had never once fired. Broad
infrastructure grants were filling the digest with low-confidence matches (the
AI-WMD grant matched 16 faculty).
**Expected effect:** `both` becomes non-zero; fewer low-value matches per grant.
**Outcome:** confirmed — `both` has been non-zero on essentially every run since
(5 of 13 delivered matches on 08-07).
**Verdict:** worked

---

## Open questions

Things we believe are true but have not verified. Each is a candidate for a
measured experiment rather than another guess.

- **Are the track-record gate and context filter firing at all?**
  `context_filtered` and `track_record_gated` read 0 on every run in the recent
  sample. Check against a run with real volume before assuming they work.
- **Is `min_confidence = 50` right?** Chosen because 65% of rows sat at 45–49.
  Never measured against faculty feedback. The 👍/👎 data is the natural
  evidence base and is not yet being used for this.
- **Do the 5 quiet foundation sources still work?** `acs` and `aacr` have
  returned nothing new in 73 days while remaining reachable. Genuinely quiet, or
  silently parsing the wrong thing? `gold_foundation` returned 0 items on
  2026-08-07 with a rising zero counter.
- **Does synopsis enrichment (2026-08-07) change the right things?** It should
  raise `raw` and improve semantic quality. If `raw` jumps but `keep%` collapses,
  the floor needs revisiting for the new score distribution.

## Don't retry these

- **A confidence multiplier on top of the shared floor** — closed the semantic
  channel entirely on 2026-05-28. Use a channel-specific floor instead.
- **Substring matching for agency/topic filters** — `epa` inside "DEPArtment".
  Word boundaries are required (2026-06-16).
- **`\b`-terminated regex alternations containing word stems** — silently killed
  a dozen vocabulary terms for months (2026-08-07). Stems need a leading boundary
  only.
- **Blocking on common English words in titles** — `education` cost us IHS health
  research grants twice, four months apart, before the cause was properly fixed.
- **Trusting a parameter because the diagnostic prints it** — `min_idf_for_match`
  was reported for weeks while unenforced.
