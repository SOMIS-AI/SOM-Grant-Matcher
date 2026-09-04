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

### 2026-09-04 — Self-reported keywords were being truncated out of the embedding
**Status:** live
**Commit:** `2064323`
**Change:** `_merge_keywords()` gains `prepend`, and Pass 8b (faculty
self-reported, from the Eval App campaign) now uses it. Their keywords go to the
FRONT of `faculty["keywords"]` instead of the end. No thresholds moved, no new
config knob.

**Why — this started as a different question.** The ask was whether
self-reported keywords and 👍/👎 feedback should get a confidence boost, on the
grounds that both come straight from the faculty member and are recent. Measuring
it first said no, and then said something better.

*The boost would not be worth it.* Across 6,365 delivered matches in the archive
(4,728 with an email recorded), 2,479 — **52%** — involve a faculty member who now
has self-reported keywords. But of those, only **5% rest wholly** on a
self-reported term and 12% partly; **84% do not touch one at all**. Scaling the
412 matches sitting in the 45–49 band by that 5% share, a ~1.10 multiplier would
move roughly **19 matches** across the entire archive. Not worth a permanent piece
of scoring complexity.

*Why reliance is so low, and what that exposed.* Self-reported keywords are high
quality but low volume — median 5 per person — against the much larger pools that
PubMed / Semantic Scholar / ClinicalTrials.gov / Europe PMC attach. Chasing that
dilution surfaced the actual fault:

- Pass 8b runs **last**, after every enrichment pass
- `_merge_keywords` **appended**: `faculty["keywords"] = existing + added`
- `embedder.faculty_to_text()` embeds only **`keywords[:40]`**

So for any faculty member already carrying 40+ enrichment keywords, their own
explicit, dated statement of what they research **never reached the semantic
vector at all** — truncated out, while machine-derived MeSH terms kept their
place. The most authoritative input was last in line and first to be dropped.

**Measured effect.** On a simulated profile with 2 UMSOM profile keywords, 55
enrichment keywords and 3 self-reported ones, against a grant squarely in the
self-reported area:

```
                self-reported inside embedded top-40    cosine vs matched grant
BEFORE (append)              0 of 3                            0.1852
AFTER  (prepend)             3 of 3                            0.3991   (+0.2138)
```

A grant that was nowhere near the 0.40 semantic threshold lands essentially on
it. Compare that with the ~19 matches the multiplier would have produced.

**Expected effect:** stronger, more current semantic vectors for the 846 faculty
with self-reported keywords (up from 349 before today's import). Keyword-channel
behaviour is unchanged — this only alters list order, and existing keywords keep
their positions, so no established list is reshuffled. Attribution in
`keywords_by_source` and dedup are unaffected, both verified.

**Takes effect on the next scrape** — the next weekly auto-rescrape, or
immediately with `FORCE_SCRAPE=true`. Embeddings regenerate when the profile text
changes, so this needs a rescrape to show up, not just a restart.

**Outcome:** *pending.*
**Verdict:** too early

**Feedback deliberately left out of scoring.** Auto-boosting a faculty member's
future confidence because they clicked 👍 is a trap while the verdict set is
n≈0 and unvalidated: it would mean tuning on a signal nobody has inspected. The
`rater=digest` verdicts in particular are third parties rating someone else's
fit and must never be fed back automatically. The comments field is the valuable
part, and its value is diagnostic rather than arithmetic — "this needs a patient
registry, I am a bench scientist" tells you to build a filter, not to nudge a
multiplier. Revisit once the floor decision has been made on real data.

### 2026-09-04 — Rarity is not specificity: geographic filler, and a single-keyword penalty
**Status:** live
**Commit:** `8027664`
**Change:** two things, one vocabulary and one structural.

*1. Vocabulary.* `stop_words` 163 → 168: `united states`, `among`, `free`,
`network`, `biology`. `context_dependent_terms` 50 → 55: `older`,
`older adults`, `young`, `young adults`, `youth` — plain gaps in the population
family that has held `adult`/`adults`/`adolescent` since June.

*2. Structural.* New `matching.single_keyword_multiplier: 0.85`, applied in
`_compute_confidence` when a match has exactly one matched keyword AND
`match_type == "keyword"`. Deliberately excludes `both`-type matches: those have
independent semantic corroboration, so the penalty targets lone *keywords*, not
lone *evidence*. Recorded in the diagnostic `params` block. 1.0 disables it.

**Why:** a reviewer flagged James E. Wiseman (Surgery) matching "Emergency
Citrus Disease Research and Extension Pre-Applications" at **67%** on the single
keyword `united states`. Two separate faults sat behind that one row.

The first is an ordinary vocabulary gap — `national` and `federal` were already
stop-worded; the country name never was. `united states` was the sole basis of
**9 delivered matches** across the archive, including the same faculty member on
"OJP FY 2026 Special Attorneys Program Round 8" at 61%.

The second is the interesting one, and it is the same failure this log has now
recorded three times (`monte carlo` 08-22, the academic terms 08-28, this).
**IDF measures how well a keyword narrows down *which* faculty to notify — it
assumes rarity is caused by specialisation.** Usually true: `glioblastoma` is
rare because few people study it. But rarity can have another cause entirely —
being an odd thing to put on a profile, which one person happened to do. The
scorer cannot distinguish the two, so `df=1` reads as "maximally distinctive":

```
"united states"     df=1 of 1293  IDF 7.16  ->  67%
"pancreatic cancer" df=3          IDF 6.07  ->  64%
```

A meaningless phrase outscoring a precise clinical one is the clearest statement
of the problem available.

**The systematic version.** Across the archive, matches resting on ONE keyword
had a **higher** median confidence (62%, n=590) than matches backed by two or
more (59%, n=3270) — exactly backwards, because a lone rare keyword earns
maximum IDF with nothing to average it down. The most frequent lone anchors mix
the legitimate (`cancer` 109, `kidney`, `spinal cord injury`) with the
meaningless (`among`, `free`, `older`, `young adults`, `network`).

**Expected effect:** the vocabulary change removes ~140 delivered matches; the
penalty removes a further 186 (4.4% of the archive's 4,267 delivered keyword
matches). 19 grants lose their entire faculty list, all of them previously
identified false positives plus "Food and Agricultural Sciences National Needs
Graduate" and "MPS Physics". Multi-keyword and `both`-type matches are
untouched — verified.

**Why 0.85 and not 0.8.** Measured: ×0.9 drops 108 of 492 singles, ×0.85 drops
185, ×0.8 drops 278. ×0.8 was tempting but takes a lone `cancer` match at 62%
below the floor while leaving `united states` at 53% — still delivered. That
asymmetry is the tell that **the penalty cannot fix this on its own**; the
vocabulary change does the real work and the penalty corrects the
over-confidence behind it. Going harder would cost good matches to catch bad
words that should not be in the vocabulary at all.

**Outcome — measured on the very next run (09-04), and it worked.**
Single-keyword matches fell from ~15% of delivered to **2 of 42**, and both
survivors are real terms (`children's mental health` 57%, `joint task force` 80%)
rather than filler. `params` confirms the multiplier was in force. Multi-keyword
and `both`-type matches were unaffected, as designed. The land-grant patterns
also caught "1890 Institution Teaching, Research, and Extension Capacity
Building" unprompted, and `insurance` (added 08-22) fired in `context_filtered`.

**And the fourth instance appeared in that same run.** `joint task force` — 80%,
the highest-confidence single-keyword match of the day, a Neurology faculty
member on a BJA immigration grant, almost certainly scraped from committee
service on their profile. Worse than `united states` in one respect: at three
tokens it earns a phrase factor of 2.2 against a cap of 13.0, so **rare AND
multi-word is the maximum-score combination the model can assign** — and neither
property says anything about topical fit. The 0.85 penalty took it from ~94% to
80%: it helped, it did not save it, which is exactly the predicted split of
labour. Added `joint task force` and `task force` to `stop_words` (170).

The single tokens `joint` and `force` were deliberately NOT stop-worded —
`joint replacement`, `joint pain`, `temporomandibular joint`, `joint
arthroplasty`, `muscle force`, `force plate` and `ground reaction force` are all
real clinical vocabulary and were verified to survive. Exactly one match in the
whole archive is affected: the one reported.

**Verdict:** worked — penalty and vocabulary both, with the vocabulary doing the
decisive work exactly as predicted.

**The general lesson, since this is now the fourth instance.** Every one of these has
the same shape: a word rare enough to earn a high score while saying nothing
about topical fit. The scorer is not broken — the vocabulary has holes, and each
hole surfaces as a confidently wrong match. The durable fix is not a smarter
score but a cleaner word list, and the diagnostic's `context_filtered` samples
are where the next hole will show up first.

### 2026-09-03 — UMSOM staff join the match pool
**Status:** live
**Commit:** `cf52657`
**Change:** a new population, not a threshold move. Manually-entered staff
profiles (`src/staff.py`, `data/staff_profiles.json`, managed from the dashboard
Subscriptions tab) are reshaped by `as_match_profiles()` into the same dict shape
`get_faculty_profiles()` returns and appended to the match pool. Their keywords
drive the keyword channel; their free-text profile becomes `evidence_titles`,
which `embedder.faculty_to_text()` folds into the embedded sentence.

**The matching decision — staff are NOT exempt from the track-record gates.**
Staff have no publication or RePORTER footprint, so `_research_tier()` puts them
in `none`: confidence ×0.8 and hard-gated off the 12 major mechanisms (R01, U01,
P01, UM1…) plus the PI-track-record mechanisms (K12, T32…). Chosen deliberately
over exempting them: a staff member is not going to PI an R01, and the gates
already encode exactly that judgement for footprint-less faculty. Verified live —
a staff member matched a "Cancer Clinical Trials Network (U01…)" call and was
correctly gated out, while a faculty member with a PubMed + RePORTER footprint
came through at 67%.

**A trap worth recording.** `_research_tier()` returns `'unknown'` for a *falsy*
`keywords_by_source`, and `'unknown'` is explicitly "never penalize" — multiplier
1.0, and the major-mechanism gate fires only on `tier == 'none'`. The obvious
`"keywords_by_source": {}` for a person with no publications would therefore have
exempted staff from both the penalty and the gate — silently doing the exact
opposite of the decision above. Staff records carry
`{"Staff profile": [...]}` instead: non-empty, but with no evidence-bearing label
from `_EVIDENCE_SOURCE_LABELS`, which is what actually lands on `'none'`.

**Expected effect:** few matches per staff member — on the order of 0–1 a week.
That is the intended consequence of keeping the gates, not a fault. `keep%` and
the faculty-facing numbers are unaffected; staff are additive.

**The real constraint on staff matching is stop-wording, not the gates.**
`_phrase_is_all_stops()` drops any phrase whose every token is a stop word, which
is correct for faculty but takes out exactly the vocabulary staff describe their
work in: `clinical trials`, `data management` and `grant writing` are all
discarded (`clinical`, `trials`, `data`, `management`, `grant`, `writing` are all
individually stop-worded). `regulatory affairs`, `IRB submissions`,
`biostatistics`, `protocol development` and `career development` survive.

This is why the profile-text field matters more for staff than the keyword list:
the semantic channel does not consult the stop-word list at all. Measured on a
topically-matched grant, a staff profile WITH text scored cosine 0.427 (above the
0.4 threshold) while one with keywords only scored 0.160. **Guidance for whoever
writes these profiles: always fill in the profile text, and prefer specific
keywords over administrative ones.** The dashboard field placeholder says so.

**Follow-up 2026-09-04:** staff shipped weekly-only; they can now choose daily or
weekly per person, so the fan-out runs from both scheduler branches filtered by
cadence. Cadence governs EMAILS ONLY — every active staff member is matched on
every run either way, and a weekly subscriber's matches accumulate until their
digest goes out. The buckets are disjoint, so nobody is emailed twice.

**Outcome:** *pending — two staff enrolled 2026-09-03, no digest sent yet.*
**Verdict:** too early

### 2026-09-01 — Disable the AHA scraper: the site now blocks us at the edge
**Status:** live
**Commit:** `6254f3d`
**Change:** config only. `external_sources.disabled_sources` 24 → 25 entries;
`aha` added, with the diagnosis and the do-not-do list inline.

**Why:** the American Heart Association put Cloudflare bot management in front
of the entire `heart.org` domain around 2026-08-19. Every host and path returns
the same 403 — `robots.txt` included, which is what rules out a page-level or
path-level cause:

```
professional.heart.org/en/research-programs   403  server=cloudflare
www.heart.org/                                403  server=cloudflare
professional.heart.org/robots.txt             403  server=cloudflare
<title>Attention Required! | Cloudflare</title>  "Sorry, you have been blocked"
```

This is a deliberate access control by the site owner, not a broken selector,
and the scraper needs no code change: the 403 is the only failure.

**The health tracker worked correctly throughout** and is worth crediting,
because it is the reason this was caught at all. `_fetch_page` raises on 403 →
`seen_links` stays empty → the source is never marked reached → `consecutive_zeros`
climbs. It crossed `likely_broken_runs: 7` on 08-28 and stood at 12 by 08-31.
The instrumentation did its job; there was simply nothing on the far end to fix.

**Explicitly rejected: every workaround.** Spoofing a browser User-Agent,
driving a headless browser through the JS challenge, and proxying are all
evasion of a control the site owner deliberately deployed, not scraper fixes.
The UA swap is called out by name in the config comment because it is a two-line
change that *looks* like a fix — the point of writing it down is that it does
not get done later by accident.

**No legitimate substitute exists.** AHA runs its applications through
ProposalCentral, but `proposalcentral.com/GrantOpportunities.asp` is a
JS-rendered marketing shell — 0 tables, 0 opportunity rows, 0 AHA links — and
`proposal_central` is already in `disabled_sources` for that same reason since
08-04. The AHA newsroom RSS fetches fine but carries no funding content.

**Expected effect:** the standing `likely_broken` alert clears, and one dead
fetch per cycle goes away. Verified in code rather than assumed: `fetch_all_sources`
skips disabled keys before `sources_tried` increments, and both the alert loop
and `per_source_health` iterate `per_source_results`, so a disabled source
leaves no stale entry behind. No effect on matching.

**Coverage cost: effectively nil.** AHA produced 8 grants across the life of the
archive and its last new one was 2026-06-18 — two months *before* the block
began. It was already a quiet source.

**Outcome:** *pending — confirm `aha` is absent from `scraper_health.health_alerts`
in the first diagnostic after the next Web App restart.*
**Verdict:** too early

**The one route that restores coverage** is asking AHA to allowlist the matcher,
by UA string or by the Azure app's egress IP. That needs a human to ask, and it
is a reasonable request for a university research-support tool making one
low-rate daily fetch. If it is ever granted, re-enable the key — no code change
required.

### 2026-08-28 — Institution-restricted grants, and academic vocabulary as an anchor
**Status:** live
**Commit:** `a61c7d7`
**Change:** config only. Two independent fixes for the same 08-25/08-27 review.

*1. `matching.ineligible_grant_patterns` 23 → 27.* Four patterns covering the
land-grant / tribal-college family — `land-grant` (which also catches
"non-land-grant college of agriculture"), `tribal college` / `tribally
controlled`, `1890` / `1994 institution`, and the `NLGCA` acronym.

*2. `matching.context_dependent_terms` 36 → 50.* Fourteen academic /
institutional words: `faculty`, `faculty development`, `students`, `student`,
`teaching`, `curricula`, `curriculum`, `educational`, `technology`,
`engineering`, `instrumentation`, `strategic partnerships`, `food`, `online`.
Plus `other` to `stop_words` (163) — a profile-form artefact, not a topic.

**Why:** three USDA-NIFA capacity/equity programs reached digests that UMB
faculty **cannot apply to at all** — they are restricted by institution *type*:

- "Scholarships for Students at 1890 Institutions" (08-11, 1 match)
- "Tribal Colleges Education Equity Grants Program" (08-25, **9 faculty at
  52–98%**, top match on `engineering, faculty, students, teaching`)
- "Capacity Building Grants for Non-Land-Grant Colleges of Agriculture"
  (08-27, 1 match on `educational, other`)

`ineligible_grant_patterns` already screened the same category — it carries
`\bMSI\b|minority-serving institution|\bHBCU\b` — which is exactly why the gap
was easy to miss. Land-grant and tribal colleges are the USDA half of that
category and were simply never added.

The second fix addresses what put faculty on those grants in the first place.
It is the same structural bug as the 08-22 method-term change, in a different
vocabulary family: every person at a medical school is "faculty", has
"students", and works with "technology", so those words describe the setting,
not the science. Two of them, `technology` and `educational`, are the singular
forms of `technologies` and `education` — both stop-worded since June, so the
plural/singular split was already an inconsistency. The 08-27 run showed the
family surviving after the method terms were neutralised: on "Annual Program
Statement", `artificial intelligence` was correctly dropped and `technology`
became the anchor instead.

**Expected effect:** four grants stop entering the pipeline entirely (they will
appear under `skipped_grants.ineligible` with the reason, not in the digest).
A small further fall in keyword-only volume. No effect on the semantic channel;
`min_semantic_confidence` still 50.

**Validated before shipping.**
*Eligibility* — the four patterns were run against all **1,276 distinct grant
titles** in the diagnostic archive. Four hits, all correct, none already caught
by an existing pattern, zero false positives. (It also picks up "Tribal Colleges
Extension Program – Capacity Applications", which had not yet reached a digest.)
*Keyword terms* — replayed against every delivered match still live under the
current config: **17 additional rows drop**. Three grants lose their whole list
(Tribal Colleges 9/9, Non-Land-Grant Colleges 1/1, and the 08-20 CMMT remnant
1/1); the rest is 2/7 off the USDA CYFAR call, 2/4 off "Annual Program
Statement", and 1 row off each of two DoW Epilepsy runs of 22.

**A measurement trap worth recording.** Replaying the *current* config against
*old* workbooks overstates the damage badly — the May/June workbooks are full of
matches that later config changes already removed, so they show up as "would
drop" when they are long gone. It read as 407 rows (9.9%) until the replay was
re-based to count only rows still live under the config as it stands. Always
measure the increment, not the total.

**Outcome:** *pending — first run after the next Web App restart.*
**Verdict:** too early

**Observed but not changed.** Generic research-*process* terms are the same bug
again: on 08-27 the spina bifida call delivered its top match at 88% on
`data collection, long term`. Adding `data collection` / `long term` would drop
only 2 rows across the whole archive — too thin to justify on its own. Revisit
if it recurs.

### 2026-08-22 — Generic method terms can no longer anchor a keyword match
**Status:** live
**Commit:** `a48a6b4`
**Change:** config only, no code and no thresholds moved.
`matching.context_dependent_terms` 12 → 36 entries; the 24 additions are all
method / measurement / domain-agnostic words, in four groups:

| group | terms |
|---|---|
| computational & statistical method | `machine`, `machine learning`, `deep learning`, `artificial intelligence`, `simulation`, `simulations`, `modeling`, `monte carlo`, `molecular dynamics`, `statistical`, `statistics`, `prediction`, `computational` |
| physical-science measurement / generic constructs | `temperature`, `chemistry`, `structure`, `evolution` |
| environmental & agricultural | `ecology`, `spatial ecology`, `agriculture`, `agricultural` |
| economic & administrative | `insurance`, `marketing`, `risk reduction` |

Separately `special emphasis` was added to `matching.stop_words` (162 entries) —
it is funding-announcement boilerplate, never a research topic, so it should not
merely fail to anchor a match, it should never match at all.

**Why:** the 08-11..08-22 diagnostic review. 322 of 402 delivered matches (80%)
in that window were keyword-only, and the keyword channel was spending that
volume on grants outside biomedicine entirely — at *higher* confidence than it
gave the genuinely relevant ones. Three from the 08-22 workbook:

- **Condensed Matter and Materials Theory** (NSF physics) — 156 keyword hits,
  11 faculty delivered at 50–69%. Top match 69% on `machine, machine learning,
  statistical`; others on `monte carlo`, `molecular dynamics`, `chemistry,
  machine learning, temperature`.
- **Spatial Ecology and Chronic Wasting Disease Dynamics of Wild Deer** — two
  Psychiatry faculty, one at **90%** on `ecology, spatial ecology`.
- **Agriculture Risk Management Education** (USDA) — two Surgery faculty at
  **87%** on `insurance, risk reduction`, one at 66% on `special emphasis`, one
  at 65% on `agriculture, marketing`.

Meanwhile the same run gave 1 delivered match to SCORCH (opioid/HIV, 27
candidates) and 1 to Translational Maternal & Pediatric Pharmacology (39
candidates). The digest was rewarding topical distance.

Two existing mechanisms could not catch these. `max_kw_prevalence_pct: 0.08`
only suppresses terms held by >8% of the roster, and `monte carlo` is *rare*
among faculty while being generic across domains — rarity and specificity have
come apart. And the June phrase-scoring change actively **rewards** them:
`machine learning` and `monte carlo` are multi-word phrases, so they collect the
phrase bonus. That is why a physics grant outscored a muscular-dystrophy one.

Every term added was observed anchoring a delivered match on an off-topic grant
in that window. None were added speculatively.

**Expected effect:** `context_filtered` in the diagnostic rises sharply (it was
59 on 08-22). Delivered keyword-only volume falls a few percent. Grants whose
entire faculty list rested on method words disappear from the digest — a grant
with zero surviving matches is not emailed. `keep%` falls slightly. **No effect
on the semantic channel** — the context filter runs on keyword matches only,
and `min_semantic_confidence` was deliberately left at 50 pending 👍/👎 data.

**Validated by replay before shipping.** The workbooks record every delivered
match with its full matched-keyword list, so the filter was replayed against
them using the shipped config. Across all Jun–Aug workbooks, **141 of 3,842
delivered rows (3.7%) drop**, and 8 grants lose their entire faculty list:

```
2026-06-06..10  -6 each  Risk Assessment: Conducting Prison Security Audits
2026-08-22      -2       Spatial Ecology / Chronic Wasting Disease (deer)
2026-08-22      -4       Agriculture Risk Management Education
2026-08-22     -11       Condensed Matter and Materials Theory (CMMT)
```

All eight are false positives, including one the review had not spotted — a
prison-security-audit call delivered five days running in June on `risk` +
`risk assessment`.

**Accepted cost, and the thing to watch.** Two NSF calls that *are* about
computational biology lose faculty whose only overlap was AI vocabulary:
"Emerging Mathematics in Biology" (08-18) drops 16 of 27, "Bioinnovation and
Infrastructure" (08-15) drops 12 of 24. Both grants survive with 11–12 faculty.
This is the known failure mode of the rule: when the method *is* the topic, a
method-only match can be legitimate. It is accepted for now because those
dropped rows were a single cluster of faculty sharing one identical keyword pair
at one identical confidence — low discriminative value — and because the
diagnostic audits every drop under `context_filtered` with samples, so the cost
is visible next run rather than silent. If AI-in-medicine grants start arriving
empty, the fix is a per-grant exemption (method terms anchor when the grant
title itself carries the method), not deleting the terms.

**Outcome:** live and firing, confirmed 08-27. The Web App was restarted after
the 08-25 run. 08-25 and 08-26 were silent on it — `nsf_funding` returned no new
grants, so nothing with method-word matches entered the pipeline, and the only
`context_filtered` drops were on `children` from the original June set. 08-27
gave the first real test and both new families fired:

```
Annual Program Statement                       48 dropped  ['artificial intelligence']
Capacity Building Grants for Non-Land-Grant     5 dropped  ['agriculture']
```

`context_filtered` was 85 of 176 raw matches on that run. No CMMT-class grant
has appeared since 08-22 to test directly, so the "grant disappears entirely"
half is still unconfirmed.
**Verdict:** worked (partial evidence — one run, no CMMT-class grant yet)
**Follow-on:** the accepted cost flagged below showed up as predicted but
smaller than feared. On 08-27 `artificial intelligence` was correctly dropped
from "Annual Program Statement" and `technology` simply became the anchor
instead — which is what prompted the academic-vocabulary entry above.

---

#### Considered and rejected in the same review: raising `min_vocab_hits`

The 08-22 analysis also proposed tightening the grant-level relevance gate —
`_is_biomedically_relevant()` in `src/matcher.py` admits a grant on a **single**
biomedical vocabulary hit (`min_vocab_hits: int = 1`), which is how a
condensed-matter-theory call gets into the pipeline at all. Raising it to 2–3
was tested against the real grant text (Grants.gov `fetchOpportunity`) for 23
grants from the 08-15..08-22 digests, split into ones that should survive and
ones that should not. **It does not separate them:**

```
should DROP                                    distinct biomedical terms
  MPS Chemistry (NSF)                          1   ('imaging')
  MPS Physics (NSF)                            1   ('molecular')
  BJS State Justice Statistics                 1   ('drug')
  Logistical Support / counternarcotics        1   ('drug')
  Emerging Mathematics in Biology              3
  Chronic Wasting Disease (deer)               4   ('disease','pathogen','mortality','diagnostic')
  BJA Public Safety and Mental Health          4

should KEEP
  DoW Breast Cancer Breakthrough               1   ('cancer')
  Rural Communities Opioid Response - Eval     1   ('opioid')
  Tribal Communities Emerging Drug Threats     1   ('drug')
  SCORCH (opioid/HIV)                          2
  Multimodal AI for Type 1 Diabetes            3
```

The distributions overlap completely. A threshold of 2 would drop the DoW Breast
Cancer call and the Rural Opioid evaluation while **keeping** the deer grant and
both BJA calls. A threshold of 3 is worse. The count of biomedical words is not
the signal — *which* words is. The DROP set passes on domain-ambiguous
vocabulary (`imaging`, `molecular`, `drug`, `disease`, `pathogen`, `cellular`);
the KEEP set passes on disease and clinical anchors (`cancer`, `opioid`, `hiv`,
`diabet`, `maternal`, `trauma`). Note also that `drug` admits narcotics-
enforcement grants and `disease` admits veterinary ecology.

If the grant gate is revisited, the shape to try is a **weak-vocabulary tier** —
terms that cannot admit a grant on their own, structurally the same idea as
`context_dependent_terms` but one layer up — not a higher count. Two further
facts for whoever picks this up: `_biomedical_vocab_hits()` returns *occurrences*
rather than distinct terms, so `len(hits)` already overstates; and the gate at
`matcher.py:1199` runs **before** the 3,000-character truncation at
`matcher.py:1265`, so it judges on more text than keyword matching ever sees.

**Not done because it was not needed.** The context-term change above removes
every observed false positive on its own — those grants keep entering the
pipeline but deliver zero faculty, and a grant with zero matches is not emailed.

### 2026-08-19 — Instrument the semantic floor instead of moving it
**Status:** live
**Change:** no tuning change. Added diagnostic fields only —
`semantic_candidates`, `semantic_above_confidence`, `semantic_lost_to_floor`,
`semantic_lost_45_49` and `min_semantic_confidence` in the run summary, plus
per-grant `candidates` / `above_confidence` / `lost_to_floor` /
`lost_confidence_bands` in `semantic_score_distributions`. Also
`feedback_links.{enabled,configured,links_rendered_this_run}` at the top level.
View with `python tools/diag_trend.py <dir> --semantic`.
**Why:** the 08-17 review found the semantic channel has largely closed —
semantic share of delivered matches fell from 88–96% (late July) to 6–30%
(post-08-11), while keyword matches rose sharply. The obvious move was to lower
`min_semantic_confidence` back below the keyword floor. But that floor was
raised 42 → 50 deliberately on 08-04 for the faculty pilot, with a documented
rationale and an explicit revisit condition: *"once 👍/👎 data lands."* The
evidence to justify reversing it did not exist — the diagnostic recorded that
semantic candidates cleared the similarity threshold (45+ across 6 grants on
08-15) but never how many the floor then discarded.
**Expected effect:** none on matching. The next review can answer "how many good
semantic matches is the floor costing, and where do they sit?" from data
instead of inference.
**Outcome:** *pending — fields populate on the first run after deploy.*
**Verdict:** too early
**Note:** the semantic-share collapse is only partly attributable to the floor.
The 08-07 synopsis enrichment gave keyword matching far more text to hit, so
keyword volume rose independently — share would have shifted even with the floor
unchanged. Separating the two needs the new counters.

### 2026-08-17 — Review of the first 10 days under the 08-07 fixes
**Status:** measured, no change made
**Outcome — relevance filter (08-07):** working. Grants of the class previously
discarded now come through: "Improving The Capacity of Tribal Communities…",
Bureau of Global Health Security awards. The skip *rate* is not a clean
indicator — 43–71% post-fix vs 36–75% before, driven mostly by what gets posted.
**Outcome — detail enrichment (08-07):** working. Award ceiling was `N/A` on
every grant in every pre-fix workbook and is now populated (5 of 10 grants on
08-15); close dates likewise. The unpopulated remainder are NSF-scraper and
foundation grants with no Grants.gov opportunity id, so `fetchOpportunity`
cannot apply — a known limitation of non-Grants.gov sources, not a regression.
**Outcome — keyword volume:** rose sharply, as intended. `keyword_only` went
from 3–29/day pre-fix to 19–86/day. Semantic matching was expected to gain too;
it did not, which is what prompted the entry above.
**Verdict:** worked (both 08-07 fixes)

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
**Outcome:** measured over 08-08..08-15 — see the 2026-08-17 entry. Both halves
work. Award ceiling went from never populated to routinely populated; previously
skipped tribal-health and global-health grants now come through; keyword volume
rose from 3–29/day to 19–86/day. The one surprise is that semantic matching did
not gain from the new synopsis text, which is being instrumented rather than
guessed at (2026-08-19).
**Verdict:** worked
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
**Outcome:** with two more weeks of data the keep rate under floor 50 settled at
3–15% (08-08..08-15) against 16–44% under floor 45 (late July). The floor is
doing what it was set to do. Its cost falls disproportionately on the semantic
channel, because `min_semantic_confidence` was raised alongside it — semantic
confidence is similarity × 100 and MiniLM's observed ceiling is ~0.57, so a
floor of 50 admits only the top sliver. Whether that trade is right for the
pilot is the open question the 2026-08-19 instrumentation exists to answer.
**Verdict:** worked as designed — the open question is whether the design is
right, not whether it took effect.

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
**Outcome:** verified 2026-08-07 across all 69 archived runs. `context_filtered`
fired on 9 runs (peak 203 on 06-27, most recently 15 on 08-01);
`track_record_gated` fired on 11 runs (peak 22 on 06-27, most recently 3 on
08-05). Independently confirmed that the mechanism patterns match real grants:
40 of 790 archived titles trip the major-mechanism gate, 3 trip the PI gate.
**Verdict:** worked
**Note:** a 3-day sample (08-05..08-07) showed 0 for both and briefly looked like
the gates were inert. They fire only when a qualifying mechanism grant appears,
which is uncommon at 7–24 grants/day. **Don't judge a sparse gate on a few days
of low volume** — check the whole archive.

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

- ~~**Are the track-record gate and context filter firing at all?**~~
  **Resolved 2026-08-07: yes.** Both fire regularly across the 69-run archive —
  see the 2026-06-23 entry. The recent zeros were low volume, not breakage.
- ~~**Has the clinical-vs-basic filter ever fired live?**~~
  **Resolved 2026-08-17: yes** — 8 suppressions on 08-12, its first live firing.
  All gates are now confirmed working.
- **Is anyone actually collecting 👍/👎 feedback?** `feedback.form_url_template`
  is empty in `config/config.yaml`; the real value is supplied at runtime by the
  `FEEDBACK_FORM_URL` app setting in Azure, which cannot be inspected from the
  repo. If it is unset, no feedback links have ever been emailed — and the
  08-04 decision to revisit the confidence floors *"once 👍/👎 data lands"* is
  waiting on something that will never arrive. The 2026-08-19 diagnostic now
  reports `feedback_links.configured` and `links_rendered_this_run`, so the next
  run settles it. **Check this before spending more time on floor tuning.**
- **Where do the verdicts go?** The 👍/👎 links point at an external form, so the
  app never sees the responses. Any analysis of feedback-vs-match-type has to
  join the form's export against the match id
  (`email|grant#|run_date|confidence|match_type|rater`). Worth building once the
  answer to the previous question is yes.
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
