#!/usr/bin/env python3
"""
Foundation Scraper Diagnostic
==============================
Run this script on Railway to diagnose why foundation scrapers return 0 results.
Tests HTTP connectivity, HTML content, and CSS selectors for every source.

Usage (Railway shell):
  python scraper_diagnostic.py

Or add as a one-off run command in Railway.
Results are printed to stdout AND emailed to DIAGNOSTIC_RECIPIENTS (if set).
"""

import os
import re
import sys
import json
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; UMSOMGrantMatcher/1.0; "
        "+https://www.medschool.umaryland.edu)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── Every source URL + CSS selectors (matching foundation_scraper.py) ─────────

SOURCES = [
    {
        "name": "Burroughs Wellcome Fund",
        "key": "burroughs_wellcome",
        "url": "https://www.bwfund.org/funding-opportunities/",
        "selectors": ["a[href*='/grants/']", "a[href*='/funding-opportunities/']"],
    },
    {
        "name": "HHMI",
        "key": "hhmi",
        "url": "https://www.hhmi.org/programs",
        "selectors": ["a[href*='/programs/']", "a[href*='/funding/']"],
    },
    {
        "name": "American Heart Association",
        "key": "aha",
        "url": "https://professional.heart.org/en/research-programs",
        "selectors": ["a[href*='research-programs']", "a[href*='funding']"],
    },
    {
        "name": "American Cancer Society",
        "key": "acs",
        "url": "https://www.cancer.org/research/we-fund-cancer-research.html",
        "selectors": ["a[href*='research']", "a[href*='grant']"],
    },
    {
        "name": "Damon Runyon",
        "key": "damon_runyon",
        "url": "https://www.damonrunyon.org/for-scientists",
        "selectors": ["a[href*='award']", "a[href*='fellowship']", "a[href*='grant']"],
    },
    {
        "name": "Alex's Lemonade Stand",
        "key": "alexs_lemonade",
        "url": "https://www.alexslemonade.org/researchers-reviewers/applicants",
        "selectors": ["a[href*='grant']", "a[href*='award']", "a[href*='research']"],
    },
    {
        "name": "Doris Duke Foundation",
        "key": "doris_duke",
        "url": "https://www.dorisduke.org/grants/",
        "selectors": ["a[href*='grant']", "a[href*='program']", "a[href*='opportunity']"],
    },
    {
        "name": "RWJF",
        "key": "rwjf",
        "url": "https://www.rwjf.org/en/grants-and-funding.html",
        "selectors": ["a[href*='grant']", "a[href*='funding']", "a[href*='cfp']"],
    },
    {
        "name": "W.M. Keck Foundation",
        "key": "keck",
        "url": "https://www.wmkeck.org/grant-programs/",
        "selectors": ["a[href*='grant']", "a[href*='program']"],
    },
    {
        "name": "Beckman Foundation",
        "key": "beckman",
        "url": "https://www.beckman-foundation.org/programs/",
        "selectors": ["a[href*='program']", "a[href*='award']"],
    },
    {
        "name": "March of Dimes",
        "key": "march_of_dimes",
        "url": "https://www.marchofdimes.org/research/grants-awards.aspx",
        "selectors": ["a[href*='grant']", "a[href*='award']", "a[href*='research']"],
    },
    {
        "name": "Alzheimer's Association",
        "key": "alzheimers_assoc",
        "url": "https://www.alz.org/research/for_researchers",
        "selectors": ["a[href*='grant']", "a[href*='funding']", "a[href*='research']"],
    },
    {
        "name": "AACR",
        "key": "aacr",
        "url": "https://www.aacr.org/professionals/research-funding/",
        "selectors": ["a[href*='grant']", "a[href*='funding']", "a[href*='award']"],
    },
    {
        "name": "Simons Foundation",
        "key": "simons",
        "url": "https://www.simonsfoundation.org/funding-opportunities/",
        "selectors": ["a[href*='funding']", "a[href*='program']", "a[href*='rfp']"],
    },
    {
        "name": "Chan Zuckerberg Initiative",
        "key": "czi",
        "url": "https://chanzuckerberg.com/science/programs/",
        "selectors": ["a[href*='program']", "a[href*='rfa']", "a[href*='initiative']"],
    },
    {
        "name": "Gates Foundation",
        "key": "gates",
        "url": "https://www.gatesfoundation.org/about/how-we-work/grant-opportunities",
        "selectors": ["a[href*='grant']", "a[href*='opportunit']", "a[href*='funding']"],
    },
    {
        "name": "Wellcome Trust",
        "key": "wellcome_trust",
        "url": "https://wellcome.org/grant-funding/schemes",
        "selectors": ["a[href*='scheme']", "a[href*='grant']", "a[href*='funding']"],
    },
    {
        "name": "Koch Foundation",
        "key": "koch_foundation",
        "url": "https://www.kochfoundation.org/apply/",
        "selectors": ["a[href*='grant']", "a[href*='apply']"],
    },
    {
        "name": "Yield Giving",
        "key": "yield_giving",
        "url": "https://yieldgiving.com/",
        "selectors": ["a[href*='grant']", "a[href*='apply']"],
    },
    {
        "name": "ProposalCentral",
        "key": "proposal_central",
        "url": "https://proposalcentral.com/GrantOpportunities.asp",
        "selectors": ["a[href*='Grant']", "a[href*='opportunity']"],
    },
    {
        "name": "Science Philanthropy Alliance",
        "key": "science_philanthropy_alliance",
        "url": "https://sciencephilanthropyalliance.org/funding-opportunities/",
        "selectors": ["a[href*='funding']", "a[href*='opportunity']", "a[href*='grant']"],
    },
    {
        "name": "PND RFPs",
        "key": "pnd_rfps",
        "url": "https://philanthropynewsdigest.org/rfps",
        "selectors": ["a[href*='rfp']"],
    },
    {
        "name": "NIH Guide",
        "key": "nih_guide",
        "url": "https://grants.nih.gov/funding/searchguide/index.html",
        "alt_url": "https://grants.nih.gov/grants/guide/WeeklyData.htm",
        "selectors": ["a[href*='notice']", "a[href*='PA-']", "a[href*='RFA-']", "a[href*='NOT-']"],
    },
    {
        "name": "ARPA-H",
        "key": "arpa_h",
        "url": "https://arpa-h.gov/research-and-funding",
        "selectors": ["a[href*='program']", "a[href*='funding']", "a[href*='research']"],
    },
    {
        "name": "DoD CDMRP",
        "key": "dod_cdmrp",
        "url": "https://cdmrp.army.mil/funding/prgdefault",
        "selectors": ["a[href*='funding']", "a[href*='prgdefault']", "a[href*='program']"],
    },
    {
        "name": "NSF Funding",
        "key": "nsf_funding",
        "url": "https://new.nsf.gov/funding/opportunities",
        "alt_url": "https://www.nsf.gov/funding/opportunities",
        "selectors": ["a[href*='opportunit']", "a[href*='funding']"],
    },
    {
        "name": "Stanford RMG",
        "key": "stanford_rmg",
        "url": "https://med.stanford.edu/rmg/funding.html",
        "selectors": ["a[href*='funding']", "a[href*='grant']", "a[href*='opportunity']"],
    },
    {
        "name": "UCLA DGSOM",
        "key": "ucla_dgsom",
        "url": "https://medschool.ucla.edu/research/research-funding",
        "selectors": ["a[href*='funding']", "a[href*='grant']", "a[href*='opportunity']"],
    },
    {
        "name": "Miami Miller",
        "key": "miami_miller",
        "url": "https://med.miami.edu/research/funding-opportunities",
        "selectors": ["a[href*='funding']", "a[href*='opportunity']", "a[href*='grant']"],
    },
    {
        "name": "AAMC Grants",
        "key": "aamc_grants",
        "url": "https://www.aamc.org/what-we-do/mission-areas/medical-education/funding-opportunities",
        "selectors": ["a[href*='grant']", "a[href*='funding']", "a[href*='award']"],
    },
]


def check_js_rendered(html: str) -> bool:
    """Heuristic: is this page likely JavaScript-rendered with no real content?"""
    soup = BeautifulSoup(html, "lxml")
    # Remove script/style tags
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(strip=True)
    # If very little text content, likely JS-rendered
    return len(text) < 500


def test_source(source: dict) -> dict:
    """Test a single source and return diagnostic info."""
    result = {
        "name": source["name"],
        "key": source["key"],
        "url": source["url"],
        "status": None,
        "status_code": None,
        "content_length": None,
        "likely_js_rendered": None,
        "selector_matches": {},
        "total_matches": 0,
        "sample_links": [],
        "error": None,
        "redirect_url": None,
    }

    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=20,
                            allow_redirects=True)
        result["status_code"] = resp.status_code
        result["content_length"] = len(resp.text)
        result["redirect_url"] = resp.url if resp.url != source["url"] else None

        if resp.status_code != 200:
            result["status"] = f"HTTP {resp.status_code}"
            return result

        result["likely_js_rendered"] = check_js_rendered(resp.text)

        soup = BeautifulSoup(resp.text, "lxml")

        # Test each CSS selector
        all_links = set()
        for selector in source["selectors"]:
            matches = soup.select(selector)
            valid = []
            for a in matches:
                title = re.sub(r"\s+", " ", a.get_text(strip=True))
                href = a.get("href", "")
                if title and len(title) >= 10:
                    link = urljoin(source["url"], href)
                    valid.append({"title": title[:80], "link": link})
                    all_links.add(link)
            result["selector_matches"][selector] = len(valid)
            if valid and not result["sample_links"]:
                result["sample_links"] = valid[:3]

        result["total_matches"] = len(all_links)
        result["status"] = "OK" if all_links else "NO MATCHES"

    except requests.exceptions.ConnectTimeout:
        result["status"] = "CONNECT TIMEOUT"
        result["error"] = "Connection timed out — site may be blocked from Railway"
    except requests.exceptions.ConnectionError as e:
        result["status"] = "CONNECTION ERROR"
        result["error"] = str(e)[:200]
    except requests.exceptions.Timeout:
        result["status"] = "READ TIMEOUT"
        result["error"] = "Response timed out"
    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = str(e)[:200]

    # Try alt_url if primary failed
    if result["total_matches"] == 0 and source.get("alt_url"):
        try:
            resp = requests.get(source["alt_url"], headers=HEADERS, timeout=20)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                for selector in source["selectors"]:
                    matches = soup.select(selector)
                    valid = [a for a in matches
                             if a.get_text(strip=True) and len(a.get_text(strip=True)) >= 10]
                    if valid:
                        result["status"] = f"OK (alt_url: {source['alt_url']})"
                        result["total_matches"] += len(valid)
        except Exception:
            pass

    return result


def format_report(results: list) -> str:
    """Format results as a readable report."""
    lines = []
    lines.append("=" * 70)
    lines.append("FOUNDATION SCRAPER DIAGNOSTIC REPORT")
    lines.append(f"Run at: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    lines.append("=" * 70)
    lines.append("")

    # Summary
    ok = sum(1 for r in results if r["total_matches"] > 0)
    no_match = sum(1 for r in results if r["status"] == "NO MATCHES")
    js_rendered = sum(1 for r in results if r["likely_js_rendered"])
    http_fail = sum(1 for r in results if r["status_code"] and r["status_code"] != 200)
    conn_fail = sum(1 for r in results if r["status"] in ("CONNECT TIMEOUT", "CONNECTION ERROR", "READ TIMEOUT"))

    lines.append(f"SUMMARY: {len(results)} sources tested")
    lines.append(f"  Working (found links):    {ok}")
    lines.append(f"  Page loads, no matches:   {no_match}")
    lines.append(f"  Likely JS-rendered:       {js_rendered}")
    lines.append(f"  HTTP errors (non-200):    {http_fail}")
    lines.append(f"  Connection failures:      {conn_fail}")
    lines.append("")

    # Categorized results
    categories = {
        "WORKING (found grant links)": [r for r in results if r["total_matches"] > 0],
        "PAGE LOADED BUT NO SELECTOR MATCHES": [r for r in results if r["status"] == "NO MATCHES" and not r["likely_js_rendered"]],
        "LIKELY JS-RENDERED (needs browser/API)": [r for r in results if r["likely_js_rendered"] and r["total_matches"] == 0],
        "CONNECTION/HTTP FAILURES": [r for r in results if r["status"] in ("CONNECT TIMEOUT", "CONNECTION ERROR", "READ TIMEOUT", "ERROR") or (r["status_code"] and r["status_code"] != 200)],
    }

    for cat_name, cat_results in categories.items():
        if not cat_results:
            continue
        lines.append("-" * 70)
        lines.append(f"{cat_name} ({len(cat_results)} sources)")
        lines.append("-" * 70)

        for r in cat_results:
            lines.append(f"\n  {r['name']} ({r['key']})")
            lines.append(f"    URL: {r['url']}")
            lines.append(f"    Status: {r['status']} | HTTP {r['status_code']} | {r['content_length']} bytes")
            if r["redirect_url"]:
                lines.append(f"    Redirected to: {r['redirect_url']}")
            if r["likely_js_rendered"]:
                lines.append(f"    ** Likely JavaScript-rendered (minimal text in HTML)")
            if r["selector_matches"]:
                for sel, count in r["selector_matches"].items():
                    lines.append(f"    Selector '{sel}': {count} matches")
            if r["sample_links"]:
                lines.append(f"    Sample links found:")
                for s in r["sample_links"]:
                    lines.append(f"      - {s['title']}")
                    lines.append(f"        {s['link']}")
            if r["error"]:
                lines.append(f"    Error: {r['error']}")
        lines.append("")

    lines.append("=" * 70)
    lines.append("END OF REPORT")
    lines.append("=" * 70)
    return "\n".join(lines)


def email_report(report: str):
    """Email the report if Gmail credentials are available."""
    sender = os.environ.get("GMAIL_SENDER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    recipients = os.environ.get("DIAGNOSTIC_RECIPIENTS") or os.environ.get("ALERT_RECIPIENTS")

    if not all([sender, password, recipients]):
        print("\n[No email credentials found — printing report only]")
        return

    recipient_list = [r.strip() for r in recipients.split(",") if r.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Grant Matcher] Scraper Diagnostic Report — {time.strftime('%B %d, %Y')}"
    msg["From"] = sender
    msg["To"] = ", ".join(recipient_list)
    msg.attach(MIMEText(report, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, recipient_list, msg.as_string())
        print(f"\nReport emailed to: {', '.join(recipient_list)}")
    except Exception as e:
        print(f"\nEmail failed: {e}")


def main():
    print("Foundation Scraper Diagnostic")
    print(f"Testing {len(SOURCES)} sources...\n")

    results = []
    for i, source in enumerate(SOURCES, 1):
        print(f"  [{i:2d}/{len(SOURCES)}] {source['name']:<35s} ", end="", flush=True)
        result = test_source(source)
        status = result["status"]
        matches = result["total_matches"]
        js = " [JS]" if result["likely_js_rendered"] else ""
        print(f"{status}{js} ({matches} links)")
        results.append(result)
        time.sleep(1)  # Rate limit

    report = format_report(results)
    print("\n" + report)

    # Save to file
    try:
        with open("data/scraper_diagnostic.txt", "w") as f:
            f.write(report)
        with open("data/scraper_diagnostic.json", "w") as f:
            json.dump(results, f, indent=2, default=str)
        print("\nSaved to data/scraper_diagnostic.txt and .json")
    except Exception as e:
        print(f"\nCould not save to file: {e}")

    email_report(report)


if __name__ == "__main__":
    main()
