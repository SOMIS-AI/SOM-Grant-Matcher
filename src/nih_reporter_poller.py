"""
NIH RePORTER & Federal RePORTER API Pollers
============================================
Fetches active funding opportunities from:
  1. NIH RePORTER (api.reporter.nih.gov) — NIH-specific FOAs, RFAs, PAs
  2. Federal RePORTER (api.federalreporter.nih.gov) — multi-agency federal awards

These supplement Grants.gov with richer NIH detail and broader federal coverage.
"""

import json
import logging
import time
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "UMSOMGrantMatcher/1.0",
}


# ── NIH RePORTER ──────────────────────────────────────────────────────────────

NIH_REPORTER_SEARCH_URL = "https://api.reporter.nih.gov/v2/projects/search"

def fetch_nih_reporter_grants(seen_ids: set, max_results: int = 100) -> list:
    """
    Fetch recently-funded NIH projects from the RePORTER API.
    Looks for projects with budget start dates in the last 30 days,
    which indicates newly active funding opportunities.
    """
    logger.info("Fetching from NIH RePORTER API...")

    today = datetime.utcnow()
    thirty_days_ago = today - timedelta(days=30)

    payload = {
        "criteria": {
            "fiscal_years": [today.year],
            "award_notice_date": {
                "from_date": thirty_days_ago.strftime("%Y-%m-%d"),
                "to_date": today.strftime("%Y-%m-%d"),
            },
            "newly_added_projects_only": True,
        },
        "include_fields": [
            "ApplId", "ProjectTitle", "AbstractText", "FoaNumber",
            "AgencyIcFundings", "Organization", "AwardAmount",
            "ProjectStartDate", "ProjectEndDate", "PrincipalInvestigators",
            "AgencyCode", "ProjectDetailUrl",
        ],
        "offset": 0,
        "limit": max_results,
        "sort_field": "ProjectStartDate",
        "sort_order": "desc",
    }

    try:
        resp = requests.post(
            NIH_REPORTER_SEARCH_URL, headers=HEADERS, json=payload, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"NIH RePORTER API request failed: {e}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"NIH RePORTER: could not parse response: {e}")
        return []

    results = data.get("results", [])
    logger.info(f"NIH RePORTER returned {len(results)} projects")

    new_grants = []
    for project in results:
        appl_id = str(project.get("appl_id", ""))
        grant_id = f"nih-reporter-{appl_id}"

        if not appl_id or grant_id in seen_ids:
            continue

        # Extract agency info
        agency_parts = []
        for ic in (project.get("agency_ic_fundings") or []):
            name = ic.get("name") or ic.get("abbreviation") or ""
            if name:
                agency_parts.append(name)
        agency = "; ".join(agency_parts) if agency_parts else (
            project.get("agency_code") or "NIH"
        )

        # Extract organization name
        org = project.get("organization") or {}
        org_name = org.get("org_name", "")

        title = project.get("project_title") or "Untitled NIH Project"
        abstract = project.get("abstract_text") or ""
        foa = project.get("foa_number") or ""

        grant = {
            "id": grant_id,
            "source": "NIH RePORTER",
            "title": title,
            "agency": agency,
            "number": foa,
            "synopsis": abstract[:2000] if abstract else "",
            "close_date": "",  # RePORTER shows funded projects, not deadlines
            "open_date": project.get("project_start_date") or "",
            "award_ceiling": str(project.get("award_amount") or ""),
            "link": (
                project.get("project_detail_url")
                or f"https://reporter.nih.gov/project-details/{appl_id}"
            ),
            "searchable_text": f"{title} {abstract}".lower(),
        }
        new_grants.append(grant)

    logger.info(f"NIH RePORTER: {len(new_grants)} new (unseen) grants")
    return new_grants


# ── Federal RePORTER ──────────────────────────────────────────────────────────

FED_REPORTER_SEARCH_URL = (
    "https://api.federalreporter.nih.gov/v1/Projects/search"
)

def fetch_federal_reporter_grants(seen_ids: set, max_results: int = 100) -> list:
    """
    Fetch recently-funded projects from the Federal RePORTER API.
    Covers NIH + CDC + AHRQ + HRSA + ACF and other federal agencies.
    """
    logger.info("Fetching from Federal RePORTER API...")

    today = datetime.utcnow()
    year = today.year

    # Federal RePORTER uses query-string params, not JSON body
    params = {
        "query": f"fy:{year}$dateFrom:01/01/{year}$dateTo:{today.strftime('%m/%d/%Y')}",
        "offset": 1,
        "limit": max_results,
    }

    try:
        resp = requests.get(
            FED_REPORTER_SEARCH_URL, params=params, headers={
                "User-Agent": "UMSOMGrantMatcher/1.0",
                "Accept": "application/json",
            }, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Federal RePORTER API request failed: {e}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Federal RePORTER: could not parse response: {e}")
        return []

    items = data.get("items", [])
    logger.info(f"Federal RePORTER returned {len(items)} projects")

    new_grants = []
    for item in items:
        proj_id = str(item.get("projectId", "") or item.get("projectNumber", ""))
        grant_id = f"fed-reporter-{proj_id}"

        if not proj_id or grant_id in seen_ids:
            continue

        title = item.get("title") or "Untitled Federal Project"
        abstract = item.get("abstract") or ""
        agency = item.get("agency") or item.get("ic", {}).get("name", "") or ""
        org = item.get("orgName") or ""
        amount = item.get("totalCostAmount") or item.get("fy_total_cost") or ""

        grant = {
            "id": grant_id,
            "source": "Federal RePORTER",
            "title": title,
            "agency": agency,
            "number": item.get("projectNumber") or "",
            "synopsis": abstract[:2000] if abstract else "",
            "close_date": "",
            "open_date": item.get("budgetStartDate") or "",
            "award_ceiling": str(amount),
            "link": f"https://reporter.nih.gov/project-details/{proj_id}",
            "searchable_text": f"{title} {abstract}".lower(),
        }
        new_grants.append(grant)

    logger.info(f"Federal RePORTER: {len(new_grants)} new (unseen) grants")
    return new_grants


# ── Combined entry point ──────────────────────────────────────────────────────

def fetch_all_reporter_grants(seen_ids: set, config: dict) -> list:
    """
    Fetch from both NIH RePORTER and Federal RePORTER APIs.
    Returns combined list of normalised grant dicts.
    """
    reporter_cfg = config.get("nih_reporter", {})
    max_results = reporter_cfg.get("max_results_per_check", 100)

    all_grants = []

    if reporter_cfg.get("enabled", True):
        grants = fetch_nih_reporter_grants(seen_ids, max_results)
        all_grants.extend(grants)
        time.sleep(1)  # respect rate limit

    if reporter_cfg.get("federal_reporter_enabled", True):
        grants = fetch_federal_reporter_grants(seen_ids, max_results)
        all_grants.extend(grants)

    logger.info(
        f"RePORTER APIs total: {len(all_grants)} new grants "
        f"(NIH + Federal combined)"
    )
    return all_grants
