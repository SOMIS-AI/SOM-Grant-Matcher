"""
Grants Poller — Multi-Source Grant Fetcher
==========================================
Fetches newly posted grant opportunities from multiple sources:
  1. Grants.gov API (federal grants — existing)
  2. NIH RePORTER & Federal RePORTER APIs
  3. External sources: foundations, portals, listing services, med school pages

API response structure for Grants.gov (typical):
  { "errorcode": 0, "msg": "...", "token": "...",
    "data": { "searchParams": {...}, "hitCount": N, "oppHits": [...grants...] } }
"""

import html
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

try:
    from atomic_io import atomic_write_json
except ImportError:
    atomic_write_json = None

try:
    from matcher import record_grants_fetch_stats
except ImportError:
    record_grants_fetch_stats = None

try:
    from nih_reporter_poller import fetch_all_reporter_grants
except ImportError:
    fetch_all_reporter_grants = None
    logger.warning("nih_reporter_poller not available — NIH/Federal RePORTER disabled")

try:
    from foundation_scraper import fetch_all_external_grants
except ImportError:
    fetch_all_external_grants = None
    logger.warning("foundation_scraper not available — external sources disabled")

GRANTS_API_URL = "https://api.grants.gov/v1/api/search2"
GRANT_DETAIL_URL = "https://www.grants.gov/search-results-detail/{opp_id}"

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "UMSOMGrantMatcher/1.0"
}


# How long a seen id is remembered. Grants.gov re-windows only the last 2 days
# (oppAge=2) and foundation pages list catalogs for months, so a year of
# retention makes re-emails effectively impossible while keeping the file
# bounded by age (~5-6k entries/yr at current volume) instead of by count.
SEEN_RETENTION_DAYS = 365


def _load_seen_timestamps(seen_file: str) -> dict:
    """Return {grant_id: first_seen_iso} from disk, tolerating both formats.

    New format: {"seen": {id: iso, ...}}. Legacy format: {"seen_ids": [...]}
    (no timestamps — those ids get stamped 'now' on the next save, which just
    restarts their retention clock; harmless).
    """
    path = Path(seen_file)
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data.get("seen"), dict):
                return dict(data["seen"])
            return {gid: None for gid in data.get("seen_ids", [])}
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not read seen grants file: {e}")
    return {}


def load_seen_grants(seen_file: str) -> set:
    return set(_load_seen_timestamps(seen_file))


def save_seen_grants(seen_file: str, seen_ids: set):
    """Persist seen ids with first-seen timestamps, pruning by AGE.

    The previous implementation kept `list(seen_ids)[-10000:]` — but seen_ids
    is a set, whose iteration order is arbitrary, so past 10k ids the slice
    dropped a RANDOM subset (including ids added that same run). Any dropped
    grant still listed at its source resurfaced as "new" and was re-emailed to
    all recipients — the same failure family as the 2026-06-06..08 duplicate
    digests. Age-based pruning is recency-correct and still bounds the file.
    """
    now = datetime.utcnow()
    prev = _load_seen_timestamps(seen_file)
    cutoff = now - timedelta(days=SEEN_RETENTION_DAYS)

    seen, pruned = {}, 0
    for gid in seen_ids:
        ts = prev.get(gid) or now.isoformat()
        try:
            if datetime.fromisoformat(ts) < cutoff:
                pruned += 1
                continue
        except (ValueError, TypeError):
            ts = now.isoformat()  # unparseable stamp — keep the id, restart its clock
        seen[gid] = ts
    if pruned:
        logger.info(f"Seen-grants: pruned {pruned} id(s) older than {SEEN_RETENTION_DAYS}d")

    # Also write the legacy "seen_ids" list so a rollback to an older image
    # (which reads only seen_ids) still sees the full set instead of an empty
    # one — an empty set would re-email the entire catalog. Remove once the
    # timestamped format has been in production long enough to trust.
    payload = {
        "seen": seen,
        "seen_ids": list(seen.keys()),
        "updated_at": now.isoformat(),
    }
    if atomic_write_json:
        atomic_write_json(seen_file, payload)
    else:  # fallback if atomic_io is unavailable — preserves old behavior
        path = Path(seen_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(payload, f)


def build_search_payload(statuses: list, max_results: int) -> dict:
    return {
        "oppStatuses": "|".join(statuses),
        "rows": max_results,
        "startRecordNum": 0,
        "oppAge": 2,
        "sortBy": "openDate|desc"
    }


def extract_opps(data: dict) -> list:
    """
    Robustly extract the list of grant opportunities from the API response.
    Handles structural variations in what 'data' contains.
    """
    inner = data.get("data")

    if inner is None:
        logger.error(f"No 'data' key in API response. Top-level keys: {list(data.keys())}")
        return []

    # Normal case: data is a dict with oppHits
    if isinstance(inner, dict):
        opps = inner.get("oppHits")
        if isinstance(opps, list):
            return opps
        # Log what keys are present to help diagnose
        logger.error(f"'oppHits' missing in data dict. data keys: {list(inner.keys())}, hitCount: {inner.get('hitCount')}")
        return []

    # Fallback: data is a list — scan for a sub-list of grant dicts
    if isinstance(inner, list):
        for item in inner:
            if isinstance(item, list) and item and isinstance(item[0], dict) and "id" in item[0]:
                logger.info(f"Found grants list inside data list ({len(item)} items)")
                return item
        logger.error(f"data is a list but no grant sub-list found. len={len(inner)}, types={[type(x).__name__ for x in inner[:5]]}")
        return []

    logger.error(f"Unexpected type for 'data': {type(inner).__name__}")
    return []


def fetch_new_grants(config: dict, seen_ids: set = None, save: bool = True) -> list:
    """Fetch new Grants.gov opportunities.

    seen_ids: optional shared seen-set. When provided it is used for filtering
      and MUTATED with the newly-seen ids (so the caller owns persistence).
      When None (legacy/standalone use), the set is loaded from disk here.
    save: persist the updated set from this function. fetch_all_sources passes
      save=False and commits once, later — see commit_seen_grants().
    """
    api_url = config["grants"]["api_url"]
    seen_file = config["grants"]["seen_grants_file"]
    max_results = config["grants"]["max_results_per_check"]
    statuses = config["grants"]["statuses"]

    if seen_ids is None:
        seen_ids = load_seen_grants(seen_file)
    logger.info(f"Checking Grants.gov API (tracking {len(seen_ids)} seen grants)")

    payload = build_search_payload(statuses, max_results)

    def _record_failure(status):
        # Record fetch health even when the call fails, so the diagnostic can
        # distinguish a broken Grants.gov from a genuinely quiet (0-new) run.
        if record_grants_fetch_stats:
            record_grants_fetch_stats(
                grants_retrieved=0, new_grants=0,
                seen_total=len(seen_ids), status=status,
            )

    try:
        resp = requests.post(api_url, headers=HEADERS, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Grants.gov API request failed: {e}")
        _record_failure(f"request_failed: {str(e)[:120]}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Could not parse Grants.gov API response: {e}")
        _record_failure("json_decode_error")
        return []

    # Log errorcode so we know if the API itself reported a problem
    errorcode = data.get("errorcode", -1)
    if errorcode != 0:
        logger.error(f"Grants.gov API returned errorcode={errorcode}, msg={data.get('msg')}")
        _record_failure(f"api_errorcode_{errorcode}")
        return []

    raw_opps = extract_opps(data)
    logger.info(f"Retrieved {len(raw_opps)} grant opportunities")

    new_grants = []
    newly_seen_ids = set()

    for opp in raw_opps:
        if not isinstance(opp, dict):
            continue

        opp_id = str(opp.get("id") or "")
        if not opp_id or opp_id in seen_ids:
            continue

        # Decode HTML entities at INGEST so all downstream consumers work from
        # real Unicode text. Grants.gov titles/synopses arrive with entities
        # ("NIH Director&rsquo;s..."), which previously (a) injected junk tokens
        # (rsquo, amp) into matching, (b) rendered as literal "&rsquo;" in the
        # escaped personalized emails, and (c) landed raw in Excel cells.
        title    = html.unescape(opp.get("title") or "Untitled")
        synopsis = html.unescape(opp.get("synopsis") or opp.get("description") or "")
        grant = {
            "id": opp_id,
            "title": title,
            "agency": html.unescape(opp.get("agency") or opp.get("agencyCode") or ""),
            "number": opp.get("number") or "",
            "synopsis": synopsis,
            "close_date": opp.get("closeDate") or "",
            "open_date": opp.get("openDate") or "",
            "award_ceiling": opp.get("awardCeiling") or "",
            "link": GRANT_DETAIL_URL.format(opp_id=opp_id),
            "searchable_text": f"{title} {synopsis}".lower()
        }

        new_grants.append(grant)
        newly_seen_ids.add(opp_id)

    seen_ids |= newly_seen_ids   # mutate in place — shared with fetch_all_sources
    if save:
        save_seen_grants(seen_file, seen_ids)

    logger.info(f"Found {len(new_grants)} new (unseen) grants")
    if record_grants_fetch_stats:
        record_grants_fetch_stats(
            grants_retrieved=len(raw_opps),
            new_grants=len(new_grants),
            seen_total=len(seen_ids),
            status="ok",
        )
    return new_grants


# ── Multi-Source Orchestrator ─────────────────────────────────────────────────

# Deferred seen-grants commit (see fetch_all_sources / commit_seen_grants).
_pending_seen = None


def commit_seen_grants() -> bool:
    """Persist the seen-set stashed by fetch_all_sources(defer_seen_save=True).

    Called by the pipeline AFTER matching succeeds. Deferring the save closes
    the pure-loss window where grants were marked seen at fetch time: a crash
    between fetch and match meant those grants were "seen" but never matched or
    emailed — gone forever. With the deferred commit, an aborted run simply
    re-fetches the same grants next time. Returns True if a commit happened.
    """
    global _pending_seen
    if _pending_seen is None:
        return False
    seen_file, ids = _pending_seen
    save_seen_grants(seen_file, ids)
    _pending_seen = None
    return True


def fetch_all_sources(config: dict, defer_seen_save: bool = False) -> list:
    """
    Fetch new grants from ALL configured sources:
      1. Grants.gov (existing)
      2. NIH RePORTER + Federal RePORTER APIs
      3. External sources (foundations, portals, med school pages)

    Returns a combined, de-duplicated list of new grant dicts.
    Each source uses the shared seen_grants tracker so grants are
    never reported twice regardless of which source found them.

    defer_seen_save=True stashes the updated seen-set instead of persisting it;
    the caller commits after matching via commit_seen_grants(). A single shared
    set is loaded once and passed through every source (fetch_new_grants no
    longer does its own load/save here), which also removes the reload dance
    that previously guarded against the 2026-06-06..08 double-save bug.
    """
    global _pending_seen
    _pending_seen = None  # drop any stale stash from a previously aborted run

    seen_file = config["grants"]["seen_grants_file"]
    all_new_grants = []
    source_stats = {}

    # One shared seen-set for ALL sources this run.
    seen_ids = load_seen_grants(seen_file)

    # Source 1: Grants.gov (original)
    logger.info("─── Source 1/3: Grants.gov ───")
    try:
        grants_gov = fetch_new_grants(config, seen_ids=seen_ids, save=False)
        all_new_grants.extend(grants_gov)
        source_stats["grants_gov"] = len(grants_gov)
    except Exception as e:
        logger.error(f"Grants.gov fetch failed: {e}", exc_info=True)
        source_stats["grants_gov"] = 0

    # Source 2: NIH RePORTER + Federal RePORTER
    if fetch_all_reporter_grants is not None:
        logger.info("─── Source 2/3: NIH & Federal RePORTER ───")
        try:
            reporter_grants = fetch_all_reporter_grants(seen_ids, config)
            all_new_grants.extend(reporter_grants)
            source_stats["reporter_apis"] = len(reporter_grants)
            # Track these as seen
            for g in reporter_grants:
                seen_ids.add(g["id"])
        except Exception as e:
            logger.error(f"RePORTER APIs failed: {e}", exc_info=True)
            source_stats["reporter_apis"] = 0
    else:
        logger.info("─── Source 2/3: NIH & Federal RePORTER (skipped — not available) ───")
        source_stats["reporter_apis"] = 0

    # Source 3: External sources (foundations, portals, listing services, med schools)
    if fetch_all_external_grants is not None:
        logger.info("─── Source 3/3: External Sources (foundations, portals, etc.) ───")
        try:
            external_grants = fetch_all_external_grants(seen_ids, config)
            all_new_grants.extend(external_grants)
            source_stats["external_sources"] = len(external_grants)
            # Track these as seen
            for g in external_grants:
                seen_ids.add(g["id"])
        except Exception as e:
            logger.error(f"External sources failed: {e}", exc_info=True)
            source_stats["external_sources"] = 0
    else:
        logger.info("─── Source 3/3: External Sources (skipped — not available) ───")
        source_stats["external_sources"] = 0

    # Persist (or stash) all seen IDs, including new ones from all sources.
    if defer_seen_save:
        _pending_seen = (seen_file, seen_ids)
        logger.info("Seen-grants save deferred — will commit after matching succeeds.")
    else:
        save_seen_grants(seen_file, seen_ids)

    # Summary
    total = len(all_new_grants)
    logger.info("=" * 50)
    logger.info(f"Multi-source fetch complete: {total} total new grants")
    for source, count in source_stats.items():
        logger.info(f"  {source}: {count} new grants")
    logger.info(f"  Seen grants tracker: {len(seen_ids)} total")
    logger.info("=" * 50)

    return all_new_grants
