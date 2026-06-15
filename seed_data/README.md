# `seed_data/`

Tracked-in-git data files that ship with the container image and are read by
the app at runtime. Unlike `data/` (gitignored — that's the Azure Files mount
where the running app writes its working state), files here are baked into the
deployment and updated by committing to source control.

## Files

### `eval_app_keywords.json`

Self-reported research keywords collected from UMSOM faculty via the Faculty
Eval App campaign. Keyed by lowercased email; accumulates across multiple
spreadsheet imports.

**Schema:**

```json
{
  "updated_at": "<iso-8601 UTC>",
  "sources": [
    {"filename": "...xlsx", "imported_at": "...", "stats": { ... }}
  ],
  "by_email": {
    "<lowercased-email>": {
      "name": "...",
      "department": "...",
      "keywords": ["...", "..."],
      "source_file": "<latest .xlsx that contributed this entry>",
      "updated_at": "<iso-8601 UTC>"
    }
  }
}
```

**How to add a new batch:**

1. Drop the new spreadsheet into
   `OneDrive - Blue Cap IT/Personal/UMSOM/AI/AI Grant Matcher/Faculty Eval App Research Keywords/`
   (or wherever you keep them) and tell Claude.
2. Claude runs `python -m src.eval_app_keywords <path-to-new-xlsx>` from the
   repo root. This merges the new entries into `seed_data/eval_app_keywords.json`
   (last-write-wins per email) and updates the `sources` audit list.
3. Claude commits the JSON change, pushes, merges dev → azure.
4. Restart prod (and set `FORCE_SCRAPE=true` if you want the new keywords to
   take effect immediately — otherwise they apply on the next weekly auto-rescrape).

The scrape pipeline reads this file at "Pass 8b" (after enrichment, before
embedding generation) and merges the keywords onto matching faculty by email.
Source attribution shows up in the dashboard's Faculty modal as
**"Faculty Self-Reported"**.
