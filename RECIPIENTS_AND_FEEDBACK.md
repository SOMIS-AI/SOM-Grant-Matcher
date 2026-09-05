# Recipients and Feedback

Who gets emailed, what they see, how their 👍/👎 comes back, and why each piece
is shaped the way it is.

`TUNING_LOG.md` covers **matching** changes — thresholds, vocabulary, scoring.
This file covers the **audience and feedback** side, which that log explicitly
excludes. Both exist so a decision can be re-read later instead of re-derived.

Last updated 2026-09-05.

---

## The four audiences

Everything below sends the same underlying match data. What differs is **scope**
(whose matches you see) and **trust** (how much your verdict is worth).

| Audience | Sees | Cadence | Rater value | Managed in |
|---|---|---|---|---|
| **Faculty subscription** | their own matches | daily or weekly | `self` | Dashboard → Subscriptions |
| **Department Grants Administrator** | their department's matches | daily or weekly | `admin` | Dashboard → Subscriptions |
| **SOM Research Administrator** | **every** match, school-wide | daily or weekly | `som_admin` | Dashboard → Subscriptions |
| **UMSOM Staff** | their own matches | daily or weekly | `self` | Dashboard → Subscriptions |
| Shared digest | every match, school-wide | daily + weekly | `digest` | `DAILY_RECIPIENTS` / `WEEKLY_RECIPIENTS` env vars |

### Why the rater values are separate

They are ordered by how much a verdict should be trusted, and kept distinct so
that judgement can be made **later, from the data**, rather than being baked in
now:

- **`self`** — the faculty member's own verdict on their own match. The
  strongest signal available and the one the semantic-floor decision has been
  waiting on since 2026-08-04.
- **`admin`** — a department research administrator answering on a faculty
  member's behalf. They know their own department's people well.
- **`som_admin`** — a school-wide research administrator, same on-behalf-of
  relationship but across every department. Added 2026-09-05. Likely to be the
  highest-*volume* verdicts, since these people see everything.
- **`digest`** — someone on the shared distribution list rating a match that is
  not theirs and whose owner they may not know. Useful signal, but a third
  party's read. **Filter or down-weight these before drawing any threshold
  conclusion.**

Collapsing these into one field would have made the feedback set look larger
while quietly mixing first-party verdicts with strangers' guesses.

### Why SOM Research Administrators exist separately from the shared digest

The *content* is identical to the shared weekly roundup. The value is elsewhere:

1. **Portal-managed.** Adding one needs no app-settings change and no restart,
   unlike `WEEKLY_RECIPIENTS`.
2. **Attributable.** Their verdicts carry `som_admin` rather than the anonymous
   `digest`.

Open question worth deciding eventually: whether `WEEKLY_RECIPIENTS` still earns
its place once real administrators are enrolled here. Same content, weaker
attribution.

---

## The feedback pipeline

```
digest email → 👍/👎 link → prefilled Microsoft Form → Responses tab → you read it
```

**Nothing is fed back into scoring automatically, by design.** As of 2026-09-05
the verdict set is still effectively empty and unvalidated; wiring it into
confidence before anyone has inspected it would mean tuning on a signal nobody
has looked at. `digest`-rater verdicts in particular must never be auto-applied.
Revisit once the floor decision has been made on real data.

### The match record

Question 1 of the form carries a pipe-delimited record, which is how a response
is joined back to a match:

```
email | grant_number | run_date | confidence | match_type | rater

jrivera@som.umaryland.edu|HHS-2027-IHS-PHN-0001|2026-09-04|67|both|self
```

Read it in Excel with **Data → Text to Columns → Delimited → Other: `|`**.

- **confidence** — what the matcher scored it. This is the evidence for where
  the confidence floor belongs: do people disagree more at 50–60 than at 80+?
- **match_type** — `keyword`, `semantic`, or `both`. Answers whether the
  semantic channel is worth reopening.
- **rater** — see the table above. **Always check this column before averaging.**

Two shapes that are not bugs:

- `name:Yiran Li|…` — that faculty member has no email on their profile, so the
  record falls back to their name and needs a manual lookup. Better a name than
  a blank; a blank first field was the 2026-09-04 bug that made verdicts
  unattributable. Count reported as `feedback_links.links_without_email`.
- `…|ALL|2026-09-04|-|-|self` — the "none of these are relevant" footer link,
  covering a whole email rather than one match. **Filter these out before
  averaging by confidence.**

Every field is stripped of `|` before joining, so a name like
`Sagheer | UMMS Ahmed` cannot misalign the columns.

### Form setup

Full field-by-field instructions, including the two non-obvious traps, are in
`Feedback_Form_Setup.md` (OneDrive project folder):

- **"Get Pre-filled URL" is in the `⋯` menu on the design toolbar**, not in the
  Collect responses panel.
- It is gated behind an **"Enable pre-filled answers" toggle**, and while that
  is off, Forms **silently ignores prefill parameters** — a correct link loads a
  blank form with no clue why.
- Keep the `%22` quotes around `{verdict}`. Forms quotes choice answers; strip
  them and the match record still fills while the verdict silently does not.

### Diagnostic fields

```json
"feedback_links": {
  "enabled": true,
  "configured": true,
  "links_rendered_this_run": 39,
  "links_without_email": 4
}
```

`configured` checks that the template actually carries `{match}` and `{verdict}`,
not merely that some string was set — the 2026-09-02 failure was a valid-looking
URL with no answer parameters, reported as `configured: true` over a dead
pipeline for a day. When it is false, a `problem` field names the fault, and no
links are rendered at all. A `warning` field appears if `{verdict}` looks
unquoted.

---

## Data stores

All under `data/` (the Azure Files mount, gitignored) unless marked otherwise.

| File | Holds | Updated by |
|---|---|---|
| `faculty_subscriptions.json` | faculty digest enrolments | Dashboard |
| `dept_admins.json` | department administrators | Dashboard |
| `som_admins.json` | school-wide administrators | Dashboard |
| `staff_profiles.json` | UMSOM staff + their match keywords | Dashboard |
| `email_log.json` | rolling send audit (capped) | automatic |
| `faculty_profiles.json` | the scraped roster | scrape |
| **`seed_data/eval_app_keywords.json`** | faculty self-reported keywords | `python -m src.eval_app_keywords <xlsx>` — **in git** |
| **`seed_data/faculty_emails.json`** | name → email directory | `python -m src.faculty_emails <csv>` — **in git** |

The two `seed_data/` files ship inside the container image, so they are updated
by committing, not by editing on the server.

### UMSOM Staff

Staff have no scraped profile — their entire match profile is what is typed into
the dashboard. Keywords drive the keyword channel; the free-text profile becomes
`evidence_titles`, which the embedder folds into the sentence it vectorises.

**Write the profile text.** Administrative vocabulary is heavily stop-worded on
the keyword channel — `clinical trials`, `data management` and `grant writing`
are all discarded because every token in them is a stop word — while the
semantic channel ignores the stop list entirely. Measured: a staff profile with
text scored cosine 0.427 against a matched grant; keywords only scored 0.160.

Staff are **not** exempt from the research-track-record gates: with no
publication footprint they are tier `none`, so ×0.8 confidence and gated off
R01/U01/P01 and the K12/T32 family. That is deliberate — a staff member is not
going to PI an R01 — and it means **0–1 matches per staff member per week is
normal, not a fault**.

---

## Deploying a change

This trips people up repeatedly, so:

| Change | What it needs |
|---|---|
| Config, thresholds, vocabulary, email templates, dashboard | push → **restart the Web App** |
| Anything touching faculty profiles, keywords, embeddings, emails | push → set **`FORCE_SCRAPE=true`** → restart → let the scrape finish → **clear the flag** |

**A green GitHub Actions run only publishes the image.** The Azure Web App must
be restarted to pull it. Automatic deploy is intentionally off — see the
comments in `.github/workflows/azure-deploy.yml`.

Leaving `FORCE_SCRAPE` set makes every run do a full re-scrape: slow, and it
hammers the UMSOM site daily for no benefit.

---

## Known gaps

- **~194 faculty still have no email.** Their UMSOM profile pages publish none —
  verified by fetching the pages directly, so it is not a scraper defect — and
  they are not in the directory export either. They fall back to the `name:`
  record, and **cannot receive a personalised digest at all**, because that
  fan-out indexes by email. Closing this needs a broader directory export.
- **Two names are deliberately unresolved.** `Sarah E. Woodson Smith` and
  `Brian W. Jackson` each map to two addresses in every source we have.
  Attaching someone's digest or verdict to the wrong colleague is worse than a
  blank, so ambiguity is skipped rather than tie-broken.
- **Enrolment email must match the roster email exactly** (both lowercased). A
  mismatch means that person is silently counted `faculty_skipped_no_match` and
  never hears anything. All 39 enrolments were checked clean on 2026-09-05.
- **Feedback is not used in scoring.** See above — deliberate, revisit later.
