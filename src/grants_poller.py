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

import json
import logging
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

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


def load_seen_grants(seen_file: str) -> set:
    path = Path(seen_file)
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
                return set(data.get("seen_ids", []))
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not read seen grants file: {e}")
    return set()


def save_seen_grants(seen_file: str, seen_ids: set):
    path = Path(seen_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    ids_list = list(seen_ids)[-10000:]
    with open(path, "w") as f:
        json.dump({"seen_ids": ids_list, "updated_at": datetime.utcnow().isoformat()}, f)


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


def fetch_new_grants(config: dict) -> list:
    api_url = config["grants"]["api_url"]
    seen_file = config["grants"]["seen_grants_file"]
    max_results = config["grants"]["max_results_per_check"]
    statuses = config["grants"]["statuses"]

    seen_ids = load_seen_grants(seen_file)
    logger.info(f"Checking Grants.gov API (tracking {len(seen_ids)} seen grants)")

    payload = build_search_payload(statuses, max_results)

    try:
        resp = requests.post(api_url, headers=HEADERS, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Grants.gov API request failed: {e}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Could not parse Grants.gov API response: {e}")
        return []

    # Log errorcode so we know if the API itself reported a problem
    errorcode = data.get("errorcode", -1)
    if errorcode != 0:
        logger.error(f"Grants.gov API returned errorcode={errorcode}, msg={data.get('msg')}")
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

        grant = {
            "id": opp_id,
            "title": opp.get("title") or "Untitled",
            "agency": opp.get("agency") or opp.get("agencyCode") or "",
            "number": opp.get("number") or "",
            "synopsis": opp.get("synopsis") or opp.get("description") or "",
            "close_date": opp.get("closeDate") or "",
            "open_date": opp.get("openDate") or "",
            "award_ceiling": opp.get("awardCeiling") or "",
            "link": GRANT_DETAIL_URL.format(opp_id=opp_id),
            "searchable_text": f"{opp.get('title', '')} {opp.get('synopsis', '')}".lower()
        }

        new_grants.append(grant)
        newly_seen_ids.add(opp_id)

    updated_seen = seen_ids | newly_seen_ids
    save_seen_grants(seen_file, updated_seen)

    logger.info(f"Found {len(new_grants)} new (unseen) grants")
    if record_grants_fetch_stats:
        record_grants_fetch_stats(
            grants_retrieved=len(raw_opps),
            new_grants=len(new_grants),
            seen_total=len(updated_seen),
        )
    return new_grants


# ── Multi-Source Orchestrator ─────────────────────────────────────────────────

def fetch_all_sources(config: dict) -> list:
    """
    Fetch new grants from ALL configured sources:
      1. Grants.gov (existing)
      2. NIH RePORTER + Federal RePORTER APIs
      3. External sources (foundations, portals, med school pages)

    Returns a combined, de-duplicated list of new grant dicts.
    Each source uses the shared seen_grants tracker so grants are
    never reported twice regardless of which source found them.
    """
    seen_file = config["grants"]["seen_grants_file"]
    seen_ids = load_seen_grants(seen_file)
    all_new_grants = []
    source_stats = {}

    # Source 1: Grants.gov (original)
    logger.info("─── Source 1/3: Grants.gov ───")
    try:
        grants_gov = fetch_new_grants(config)
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

    # Save all seen IDs (including new ones from all sources)
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
