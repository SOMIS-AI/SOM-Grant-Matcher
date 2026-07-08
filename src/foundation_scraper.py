"""
Foundation & External Sources Scraper — v4.2
=============================================
Rewritten to use RSS feeds, APIs, and better scraping strategies.

Previous v4.1 scrapers all returned 0 results because:
  1. Most foundation websites are JavaScript-rendered (React/Next.js)
     and requests.get() only gets the empty HTML shell.
  2. CSS selectors were speculative and didn't match actual page structure.

v4.2 Strategy:
  - Tier A: RSS/Atom feeds (NIH Guide, NSF, PND) — most reliable
  - Tier B: API / structured data (ARPA-H, DoD CDMRP)
  - Tier C: HTML scrapers with broader link-pattern matching
  - Tier D: Medical school & institutional pages

Each source has a scrape function returning a list of normalised grant dicts.
Failures in any individual source are caught so others can still proceed.
"""

import hashlib
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; UMSOMGrantMatcher/1.0; "
        "+https://www.medschool.umaryland.edu)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

REQUEST_DELAY = 2.0


# ======================================================================
# HELPERS
# ======================================================================

def _make_id(source, title, url):
    raw = f"{source}|{title}|{url}"
    return f"{source.lower().replace(' ', '-')}-{hashlib.md5(raw.encode()).hexdigest()[:12]}"


def _fetch_page(url, timeout=20):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        if len(resp.text) < 500:
            logger.warning(f"Page {url} returned only {len(resp.text)} bytes")
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return None


def _fetch_rss(url, timeout=20):
    """Fetch and parse RSS/Atom feed. Returns list of dicts."""
    try:
        resp = requests.get(url, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
        }, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Failed to fetch RSS {url}: {e}")
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        logger.warning(f"Failed to parse RSS XML from {url}: {e}")
        return []

    items = []

    # RSS 2.0
    for item in root.iter("item"):
        entry = {}
        for field, tag in [("title", "title"), ("link", "link"),
                           ("description", "description"), ("pub_date", "pubDate")]:
            el = item.find(tag)
            entry[field] = el.text.strip() if el is not None and el.text else ""
        if entry.get("title") and entry.get("link"):
            items.append(entry)

    # Atom fallback
    if not items:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry_el in root.findall(".//atom:entry", ns):
            entry = {}
            t = entry_el.find("atom:title", ns)
            lnk = entry_el.find("atom:link", ns)
            s = entry_el.find("atom:summary", ns)
            entry["title"] = t.text.strip() if t is not None and t.text else ""
            entry["link"] = lnk.get("href", "") if lnk is not None else ""
            entry["description"] = s.text.strip() if s is not None and s.text else ""
            if entry.get("title") and entry.get("link"):
                items.append(entry)

    return items


def _extract_text(soup_element):
    if soup_element is None:
        return ""
    return re.sub(r"\s+", " ", soup_element.get_text(strip=True))


def _clean_html(html_str):
    if not html_str:
        return ""
    return re.sub(r"<[^>]+>", " ", html_str).strip()


def _make_grant(source, source_id, title, link, synopsis="", agency="",
                close_date="", open_date="", award_ceiling="",
                extra_search_text=""):
    searchable = f"{title} {synopsis} {extra_search_text}".lower()
    return {
        "id": _make_id(source_id, title, link),
        "source": source,
        "title": title,
        "agency": agency or source,
        "number": "",
        "synopsis": synopsis[:2000] if synopsis else "",
        "close_date": close_date,
        "open_date": open_date,
        "award_ceiling": award_ceiling,
        "link": link,
        "searchable_text": searchable,
    }


# ======================================================================
# TIER A: RSS FEED SCRAPERS
# ======================================================================

def scrape_nih_guide(seen_ids):
    """NIH Guide for Grants and Contracts — official RSS feed."""
    items = _fetch_rss("https://grants.nih.gov/grants/guide/newsfeed/fundingopps.xml")
    if not items:
        logger.info("NIH Guide RSS: 0 items or failed")
        return []
    _mark_reached("nih_guide", len(items))  # feed reachable, even if all items are seen/notices

    grants = []
    for item in items:
        title = item["title"]
        link = item["link"]
        if not title or len(title) < 10:
            continue
        gid = _make_id("nih-guide", title, link)
        if gid in seen_ids:
            continue
        grants.append(_make_grant(
            source="NIH Guide", source_id="nih-guide", title=title,
            link=link, synopsis=_clean_html(item.get("description", "")),
            agency="NIH",
        ))
    logger.info(f"NIH Guide RSS: {len(grants)} new (from {len(items)} feed items)")
    return grants


def scrape_nsf_funding(seen_ids):
    """NSF Funding Opportunities — official RSS feed."""
    items = _fetch_rss("https://www.nsf.gov/rss/rss_www_funding.xml")
    if not items:
        items = _fetch_rss("https://new.nsf.gov/rss/rss_www_funding.xml")
    if not items:
        logger.info("NSF RSS: 0 items or failed")
        return []
    _mark_reached("nsf_funding", len(items))  # feed reachable, even if all items are seen

    grants = []
    for item in items:
        title = item["title"]
        link = item["link"]
        if not title or len(title) < 10:
            continue
        gid = _make_id("nsf", title, link)
        if gid in seen_ids:
            continue
        grants.append(_make_grant(
            source="NSF", source_id="nsf", title=title,
            link=link, synopsis=_clean_html(item.get("description", "")),
            agency="National Science Foundation",
        ))
    logger.info(f"NSF RSS: {len(grants)} new (from {len(items)} feed items)")
    return grants


def scrape_pnd_rfps(seen_ids):
    """Philanthropy News Digest — try RSS, then HTML fallback."""
    items = _fetch_rss("https://philanthropynewsdigest.org/rfps.rss")
    if not items:
        items = _fetch_rss("https://philanthropynewsdigest.org/rfps/feed")
    if not items:
        # HTML fallback
        soup = _fetch_page("https://philanthropynewsdigest.org/rfps")
        if soup:
            for a in soup.find_all("a", href=True):
                href = a.get("href", "")
                title = _extract_text(a)
                if "/rfps/" in href and title and len(title) >= 10:
                    link = urljoin("https://philanthropynewsdigest.org", href)
                    items.append({"title": title, "link": link, "description": ""})

    grants = []
    for item in items:
        title = item["title"]
        link = item["link"]
        gid = _make_id("pnd", title, link)
        if gid in seen_ids:
            continue
        grants.append(_make_grant(
            source="PND RFP Bulletin", source_id="pnd", title=title,
            link=link, synopsis=_clean_html(item.get("description", "")),
            agency="Philanthropy News Digest",
        ))
    logger.info(f"PND RFP Bulletin: {len(grants)} new opportunities found")
    return grants


# ======================================================================
# TIER B: API / STRUCTURED DATA
# ======================================================================

def scrape_arpa_h(seen_ids):
    """ARPA-H open funding opportunities."""
    urls = [
        "https://arpa-h.gov/explore-funding/open-funding-opportunities",
        "https://arpa-h.gov/research-and-funding",
    ]
    soup = None
    used_url = ""
    for url in urls:
        soup = _fetch_page(url)
        if soup and len(soup.get_text(strip=True)) > 1000:
            used_url = url
            break
        soup = None

    if not soup:
        logger.info("ARPA-H: pages appear JS-rendered, cannot scrape")
        return []

    grants = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        title = _extract_text(a)
        relevant = any(kw in href.lower() for kw in [
            "sam.gov", "/programs/", "iso", "solicitation"
        ])
        if not relevant or not title or len(title) < 10:
            continue
        if any(s in title.lower() for s in [
            "learn more", "sign up", "subscribe", "contact"
        ]):
            continue
        link = href if href.startswith("http") else urljoin(used_url, href)
        gid = _make_id("arpa-h", title, link)
        if gid in seen_ids:
            continue
        grants.append(_make_grant(
            source="ARPA-H", source_id="arpa-h", title=title, link=link,
            agency="Advanced Research Projects Agency for Health",
            extra_search_text="biomedical health research",
        ))
    logger.info(f"ARPA-H: {len(grants)} opportunities found")
    return grants


def scrape_dod_cdmrp(seen_ids):
    """DoD CDMRP — .mil domains timeout from Railway."""
    for url in [
        "https://cdmrp.army.mil/funding/prgdefault",
        "https://www.usamraa.army.mil/Pages/Resources.aspx",
    ]:
        soup = _fetch_page(url)
        if soup:
            grants = []
            for a in soup.find_all("a", href=True):
                title = _extract_text(a)
                href = a.get("href", "")
                if any(k in href.lower() for k in ["funding", "program"]):
                    if title and len(title) >= 10:
                        link = href if href.startswith("http") else urljoin(url, href)
                        gid = _make_id("cdmrp", title, link)
                        if gid not in seen_ids:
                            grants.append(_make_grant(
                                source="DoD CDMRP", source_id="cdmrp",
                                title=title, link=link,
                                agency="Department of Defense CDMRP",
                                extra_search_text="military medical research",
                            ))
            logger.info(f"DoD CDMRP: {len(grants)} opportunities found")
            return grants
    logger.info("DoD CDMRP: .mil sites unreachable — grants captured via Grants.gov")
    return []


# ======================================================================
# TIER C: HTML SCRAPERS — GENERIC ENGINE
# ======================================================================

def _is_junk_title(title):
    """
    Filter out navigation links, UI elements, and non-grant page titles
    that get picked up by the broad link scanner.
    """
    t = re.sub(r"\s+", " ", (title or "").lower()).strip(" :–—-•|\t")

    # Too short to be a real opportunity title
    if len(t) < 4:
        return True

    # Exact matches — section headers / nav / UI that are never grant titles.
    # (Includes nav junk observed from AHA/ACS/AACR/Simons portal scrapes.)
    JUNK_EXACT = {
        "learn more", "read more", "see more", "view more", "show more",
        "view all", "see all", "browse all", "load more",
        "apply now", "apply here", "apply online", "sign up", "sign in",
        "log in", "register", "subscribe", "submit",
        "back to top", "skip to content", "skip to main content",
        "home", "menu", "close", "search", "share",
        "next", "previous", "back",
        # bare section nouns
        "funding", "funding opportunity", "opportunities", "grants", "awards",
        "fellowships", "programs", "program", "resources", "overview",
        "guidelines", "eligibility", "events", "news", "about", "contact", "faq",
        "research", "research funding", "research categories",
        "acs research categories", "research program areas", "research programs",
        "researcher summits", "upcoming events", "funded research",
        "research training and fellowships", "research funding impact",
        "research findings", "research accomplishments", "total grant resources",
        "career development awards", "information for current grantees",
        "policies & procedures", "policies and procedures", "policies",
        "rfa details", "apply for funding", "grant application and review process",
        "women in aha research", "stand up to cancer", "funding partnerships",
        "aacr efforts to protect nih funding", "award instructions",
        "see application resources", "discover research findings",
    }
    if t in JUNK_EXACT:
        return True

    # Starts-with patterns — navigation verbs and UI chrome
    JUNK_PREFIXES = [
        # navigation verbs (real grant titles don't start with these)
        "view ", "see ", "discover ", "explore ", "browse ", "read ",
        "learn ", "watch ", "meet ", "find out", "apply for", "applying ",
        "partnering with", "information for", "how to apply",
        # existing
        "application guidelines", "application instructions",
        "guidelines and information", "meeting dates",
        "grants calendar", "grant calendar", "events calendar",
        "see new grantee", "tips for applicants",
        "grant recipients", "grant review process",
        "researcher & reviewer", "researchers & reviewers",
        "scientific advisory board", "advisory board members",
        "co-fund a grant", "hospital study accounts",
        "reach grant application guidelines",
        "award grant application guidelines",
        "grant agreement with",
        "crazy 8 grant agreement",
        "browse alsf funded",
        "genomic data sharing",
    ]
    for prefix in JUNK_PREFIXES:
        if t.startswith(prefix):
            return True

    # Contains patterns — generic non-grant content
    JUNK_CONTAINS = [
        "cookie policy", "privacy policy", "terms of use",
        "annual report", "press release", "newsletter signup",
        "board of directors", "staff directory",
        "funding opportunities",          # "Upcoming/Internal/… Funding Opportunities"
        "grant cycle changes", "application and review process",
        "success rates",
    ]
    for pattern in JUNK_CONTAINS:
        if pattern in t:
            return True

    # All-caps short titles are typically section headers, not grants
    if title.isupper() and len(title.split()) <= 4:
        return True

    return False


def _scrape_generic(name, source_id, url, seen_ids, link_patterns,
                    skip_words=None, agency="", extra_search=""):
    """
    Broad HTML scraper: fetches page, scans ALL <a> tags, keeps those
    whose href contains any of the link_patterns. Much more resilient
    than hardcoded CSS selectors.
    """
    soup = _fetch_page(url)
    if not soup:
        return []
    # NOTE: reachability is recorded AFTER the link scan (see _mark_reached below),
    # using the count of real grant-like links found — NOT the mere fact that the
    # page fetched. Many foundation sites are JS-rendered: requests.get() returns a
    # 200-OK shell with zero grant links. Marking such a shell "reached" would reset
    # the zero-streak and permanently mask a silently-broken scraper as "quiet".

    skip = skip_words or [
        "privacy", "contact", "about", "login", "careers",
        "donate", "news", "blog", "faq", "terms",
    ]
    grants = []
    seen_links = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not any(pat in href.lower() for pat in link_patterns):
            continue
        title = _extract_text(a)
        link = href if href.startswith("http") else urljoin(url, href)

        if not title or len(title) < 10 or link in seen_links or link == url:
            continue
        if any(s in title.lower() for s in skip):
            continue
        if _is_junk_title(title):
            continue

        seen_links.add(link)
        gid = _make_id(source_id, title, link)
        if gid in seen_ids:
            continue

        grants.append(_make_grant(
            source=name, source_id=source_id, title=title, link=link,
            agency=agency or name, extra_search_text=extra_search,
        ))

    # Record reachability only if the page exposed real grant-like links. A
    # JS-rendered shell yields 0 candidate links → NOT marked reached → after 3
    # consecutive zero-runs the health tracker correctly flags it likely_broken.
    # `seen_links` holds every link that passed the length/skip/junk filters
    # (independent of whether it was already in seen_ids), so it measures what the
    # page actually offers, not just what was new this run.
    if seen_links:
        _mark_reached(source_id, len(seen_links))

    logger.info(f"{name}: {len(grants)} opportunities found")
    return grants


# -- Foundation scrapers using generic engine --

def scrape_burroughs_wellcome(seen_ids):
    return _scrape_generic(
        "Burroughs Wellcome Fund", "bwf",
        "https://www.bwfund.org/funding-opportunities/", seen_ids,
        ["bwfund.org/funding-opportunities/", "bwfund.org/grant"],
        extra_search="biomedical career award seed grant",
    )

def scrape_hhmi(seen_ids):
    return _scrape_generic(
        "HHMI", "hhmi",
        "https://www.hhmi.org/programs", seen_ids,
        ["hhmi.org/programs/", "hhmi.org/science/"],
        agency="Howard Hughes Medical Institute",
        extra_search="biomedical research fellowship investigator",
    )

def scrape_aha(seen_ids):
    return _scrape_generic(
        "American Heart Association", "aha",
        "https://professional.heart.org/en/research-programs", seen_ids,
        ["research-programs/", "funding-opportunities", "heart.org/en/research",
         "professional.heart.org/en/research"],
        agency="American Heart Association",
        extra_search="cardiovascular heart research grant",
    )

def scrape_acs(seen_ids):
    return _scrape_generic(
        "American Cancer Society", "acs",
        "https://www.cancer.org/research/we-fund-cancer-research/apply-research-grant.html",
        seen_ids,
        ["apply-research-grant", "grant-types", "cancer.org/research/"],
        agency="American Cancer Society",
        extra_search="cancer research grant oncology",
    )

def scrape_damon_runyon(seen_ids):
    return _scrape_generic(
        "Damon Runyon", "damon-runyon",
        "https://www.damonrunyon.org/for-scientists", seen_ids,
        ["award", "fellowship", "grant", "for-scientists/"],
        agency="Damon Runyon Cancer Research Foundation",
        extra_search="cancer research fellowship",
    )

def scrape_alexs_lemonade(seen_ids):
    return _scrape_generic(
        "Alex's Lemonade Stand", "alexs-lemonade",
        "https://www.alexslemonade.org/researchers-reviewers/applicants", seen_ids,
        ["grant", "award", "researchers-reviewers"],
        agency="Alex's Lemonade Stand Foundation",
        extra_search="childhood pediatric cancer research",
    )

def scrape_doris_duke(seen_ids):
    return _scrape_generic(
        "Doris Duke Foundation", "doris-duke",
        "https://www.dorisduke.org/grants/", seen_ids,
        ["dorisduke.org/grants/", "dorisduke.org/programs/"],
        agency="Doris Duke Foundation",
        extra_search="biomedical clinical research",
    )

def scrape_rwjf(seen_ids):
    return _scrape_generic(
        "RWJF", "rwjf",
        "https://www.rwjf.org/en/grants/active-funding-opportunities.html", seen_ids,
        ["rwjf.org/en/grants/active-funding", "rwjf.org/en/grants/grant"],
        agency="Robert Wood Johnson Foundation",
        extra_search="health equity public health research",
    )

def scrape_keck(seen_ids):
    return _scrape_generic(
        "W.M. Keck Foundation", "keck",
        "https://www.wmkeck.org/research-application-process/", seen_ids,
        ["wmkeck.org/research", "wmkeck.org/socal", "wmkeck.org/our-focus"],
        extra_search="science engineering medical research",
    )

def scrape_beckman(seen_ids):
    return _scrape_generic(
        "Beckman Foundation", "beckman",
        "https://www.beckman-foundation.org/programs/", seen_ids,
        ["beckman-foundation.org/programs/", "beckman-foundation.org/award"],
        agency="Arnold & Mabel Beckman Foundation",
        extra_search="chemistry biomedical instrumentation",
    )

def scrape_march_of_dimes(seen_ids):
    return _scrape_generic(
        "March of Dimes", "march-of-dimes",
        "https://www.marchofdimes.org/research", seen_ids,
        ["marchofdimes.org/research/", "marchofdimes.org/grants"],
        extra_search="maternal infant prematurity birth defects",
    )

def scrape_alzheimers_assoc(seen_ids):
    return _scrape_generic(
        "Alzheimer's Association", "alz",
        "https://www.alz.org/research/for_researchers", seen_ids,
        ["alz.org/research/", "alz.org/grants", "alz.org/professionals",
         "alz.org/science/"],
        extra_search="alzheimer dementia neurodegenerative research",
    )

def scrape_aacr(seen_ids):
    return _scrape_generic(
        "AACR", "aacr",
        "https://www.aacr.org/professionals/research-funding/", seen_ids,
        ["aacr.org/professionals/research-funding/", "aacr.org/grants",
         "aacr.org/professionals/volunteer"],
        agency="American Association for Cancer Research",
        extra_search="cancer research grant fellowship oncology",
    )

def scrape_simons(seen_ids):
    return _scrape_generic(
        "Simons Foundation", "simons",
        "https://www.simonsfoundation.org/funding-opportunities/", seen_ids,
        ["simonsfoundation.org/funding", "simonsfoundation.org/grant"],
        extra_search="mathematics neuroscience basic science",
    )

def scrape_czi(seen_ids):
    return _scrape_generic(
        "CZI", "czi",
        "https://chanzuckerberg.com/science/science-funding/", seen_ids,
        ["chanzuckerberg.com/rfa/", "chanzuckerberg.com/science/"],
        agency="Chan Zuckerberg Initiative",
        extra_search="biomedical imaging single cell rare disease",
    )

def scrape_gates(seen_ids):
    return _scrape_generic(
        "Gates Foundation", "gates",
        "https://www.gatesfoundation.org/about/how-we-work/grant-opportunities",
        seen_ids,
        ["gatesfoundation.org/about/how-we-work/grant", "gatesfoundation.org/ideas/",
         "gatesfoundation.org/about/committed-grants/"],
        agency="Bill & Melinda Gates Foundation",
        extra_search="global health infectious disease vaccine",
    )

def scrape_wellcome_trust(seen_ids):
    return _scrape_generic(
        "Wellcome Trust", "wellcome",
        "https://wellcome.org/grant-funding/schemes", seen_ids,
        ["wellcome.org/grant-funding/", "wellcome.org/what-we-do/", "wellcome.org/funding/"],
        extra_search="biomedical infectious disease mental health UK",
    )

def scrape_koch_foundation(seen_ids):
    return _scrape_generic(
        "David H. Koch Foundation", "koch",
        "https://www.kochfoundation.org/apply/", seen_ids,
        ["kochfoundation.org/apply", "kochfoundation.org/grant"],
        extra_search="cancer research medical science",
    )

def scrape_yield_giving(seen_ids):
    return _scrape_generic(
        "Yield Giving", "yield-giving",
        "https://yieldgiving.com/", seen_ids,
        ["yieldgiving.com/"],
        extra_search="philanthropy research education health",
    )

def scrape_proposal_central(seen_ids):
    return _scrape_generic(
        "ProposalCentral", "proposal-central",
        "https://proposalcentral.com/GrantOpportunities.asp", seen_ids,
        ["GrantOpportunity", "proposalcentral.com/"],
        extra_search="biomedical research grant",
    )

def scrape_science_philanthropy_alliance(seen_ids):
    grants = _scrape_generic(
        "Science Philanthropy Alliance", "spa",
        "https://sciencephilanthropyalliance.org/funding-opportunities/", seen_ids,
        ["sciencephilanthropyalliance.org/", "funding", "opportunity"],
        extra_search="basic science research funding",
    )
    if not grants:
        grants = _scrape_generic(
            "Science Philanthropy Alliance", "spa",
            "https://sciencephilanthropyalliance.org/what-we-do/", seen_ids,
            ["sciencephilanthropyalliance.org/", "funding", "opportunity"],
            extra_search="basic science research funding",
        )
    return grants


# -- Medical school pages --

def scrape_stanford_rmg(seen_ids):
    return _scrape_generic(
        "Stanford Medicine RMG", "stanford-rmg",
        "https://med.stanford.edu/rmg/funding.html", seen_ids,
        ["med.stanford.edu/rmg/", "stanford.edu/funding"],
        agency="Stanford Medicine",
        extra_search="clinical translational research funding",
    )

def scrape_ucla_dgsom(seen_ids):
    return _scrape_generic(
        "UCLA DGSOM", "ucla-dgsom",
        "https://medschool.ucla.edu/research/research-funding", seen_ids,
        ["medschool.ucla.edu/research/", "ucla.edu/funding"],
        extra_search="clinical biomedical research funding",
    )

def scrape_miami_miller(seen_ids):
    return _scrape_generic(
        "Miami Miller SOM", "miami-miller",
        "https://med.miami.edu/research/funding-opportunities", seen_ids,
        ["med.miami.edu/research/", "miami.edu/funding"],
        extra_search="clinical biomedical research funding",
    )

def scrape_aamc_grants(seen_ids):
    return _scrape_generic(
        "AAMC Grants", "aamc",
        "https://www.aamc.org/learn-network/affinity-groups/group-educational-affairs/grants-medical-educators",
        seen_ids,
        ["aamc.org/learn-network/", "aamc.org/career-development/", "aamc.org/about-us/mission"],
        agency="AAMC",
        extra_search="medical education research grant",
    )


# -- PCORI (Patient-Centered Outcomes Research Institute) --

def scrape_pcori(seen_ids):
    """
    Scrape PCORI funding opportunities page.
    PCORI funds patient-centered comparative effectiveness research (CER),
    issuing $300M+ annually. Their funding page uses structured HTML tables
    with status labels (Open, Upcoming, Receiving Invited Applications).
    """
    url = "https://www.pcori.org/funding-opportunities"
    grants = []
    try:
        soup = _fetch_page(url)
        if not soup:
            return grants

        # Find all opportunity links in the funding table
        # Each row has a status label and a link to the announcement
        for row in soup.select("table tr, .views-row, .view-content a"):
            # Try table rows first
            link = None
            title_text = ""

            if row.name == "tr":
                cells = row.find_all("td")
                if not cells:
                    continue
                link = cells[0].find("a") if cells else None
                title_text = cells[0].get_text(strip=True) if cells else ""
            elif row.name == "a":
                link = row
                title_text = row.get_text(strip=True)

            if not link or not link.get("href"):
                continue

            href = link.get("href", "")
            # Only keep actual funding announcement links
            if "/funding-opportunities/announcement/" not in href:
                continue

            title = link.get_text(strip=True)
            if not title or len(title) < 10:
                continue

            # Clean up status prefixes from title text (e.g., "Open", "Upcoming")
            for prefix in ["Open", "Upcoming", "Closed",
                           "Receiving Invited Applications",
                           "Applications Under Review"]:
                if title.startswith(prefix):
                    title = title[len(prefix):].strip()

            full_url = href if href.startswith("http") else f"https://www.pcori.org{href}"
            grant_id = _make_id("pcori", title, full_url)

            if grant_id in seen_ids:
                continue

            # Check for junk
            if _is_junk_title(title):
                continue

            # Extract deadline from adjacent cells if in table
            close_date = ""
            if row.name == "tr":
                cells = row.find_all("td")
                if len(cells) >= 3:
                    close_date = cells[2].get_text(strip=True)  # Application Deadline column

            grants.append({
                "id": grant_id,
                "title": title,
                "agency": "PCORI",
                "number": "",
                "link": full_url,
                "close_date": close_date,
                "open_date": "",
                "synopsis": (
                    f"PCORI funding opportunity: {title}. "
                    f"Patient-centered comparative clinical effectiveness research."
                ),
                "searchable_text": (
                    f"{title} PCORI patient-centered outcomes research "
                    f"comparative clinical effectiveness CER"
                ),
                "source": "pcori",
            })

        logger.info(f"PCORI: found {len(grants)} new opportunities")
    except Exception as e:
        logger.error(f"PCORI scraper failed: {e}", exc_info=True)
    return grants


# ======================================================================
# Medical-education and physician-focused foundations (2026-06-04)
#
# Probed each candidate from the user's "educational sources" list and only
# built scrapers for the three with genuinely scrapeable opportunity listings.
# Skipped (explicit by-name notes so future-us doesn't re-attempt without new
# information):
#   - Spencer Foundation: static pages with no programmatic RFP listing;
#     grants are announced via their mailing list, not the website.
#   - ABIM Foundation: their public pages are grantee-navigation; no current
#     open RFPs visible to a non-logged-in scraper.
#   - PND / Philanthropy News Digest: Candid (parent org) moved the RFP feed
#     behind their paid Foundation Directory Online; the legacy /feeds/rfp URL
#     now redirects to a Next.js content site with general nonprofit articles,
#     NOT RFPs. The existing scrape_pnd_rfps is correspondingly disabled.
#   - Pharma IISRs (AstraZeneca, BMS, Pfizer): registration-walled portals; you
#     submit proposals to them, they don't publish open RFPs publicly.
#   - NBME Stemmler, IAMSE, FAIMER, SDRME, SACME, AAMC awards: each funds 1-3
#     small grants/year; volume too low to justify a dedicated scraper.
# ======================================================================

def scrape_ama(seen_ids):
    """American Medical Association — research grants + AMA Foundation awards.

    The AMA awards landing surfaces the Joan F. Giambalvo Fund, AMA Research
    Challenge, Excellence in Medicine awards, and other named programs.
    """
    return _scrape_generic(
        "American Medical Association", "ama",
        "https://www.ama-assn.org/about/awards", seen_ids,
        link_patterns=[
            "/about/awards/", "/topics/ama-awards", "/topics/ama-grants",
            "amafoundation.org/", "/research-challenge", "/giambalvo",
        ],
        agency="American Medical Association",
        extra_search="physician medical education research grant award",
    )


def scrape_gold_foundation(seen_ids):
    """Arnold P. Gold Foundation — humanism in healthcare awards & grants."""
    return _scrape_generic(
        "Arnold P. Gold Foundation", "gold_foundation",
        "https://www.gold-foundation.org/our-grants/", seen_ids,
        link_patterns=[
            "/programs/", "/humanism", "/grants/", "/grant-",
            "/awards/", "/award-",
        ],
        agency="Arnold P. Gold Foundation",
        extra_search="humanism in medicine residency interprofessional education",
    )


def scrape_macy_foundation(seen_ids):
    """Josiah Macy Jr. Foundation — Catalyst Awards + Recent RFAs.

    Macy posts ~1-3 RFAs/year, so this scraper is intentionally narrow and
    low-yield — most runs will return 0 new grants.
    """
    return _scrape_generic(
        "Josiah Macy Jr. Foundation", "macy_foundation",
        "https://macyfoundation.org/our-grantees/recent-rfas", seen_ids,
        link_patterns=[
            "/our-grantees/catalyst-awards", "/our-grantees/", "/rfa",
            "/funding", "/awards",
        ],
        skip_words=[
            "privacy", "contact", "login", "careers", "donate", "newsletter",
            "stories", "about", "leadership", "history",
        ],
        agency="Josiah Macy Jr. Foundation",
        extra_search="medical education health professions catalyst",
    )


# ======================================================================
# DoD CDMRP / MRDC research programs via eBRAP (program-level)
# ======================================================================

def scrape_ebrap(seen_ids):
    """U.S. DoD CDMRP / MRDC research programs from the public eBRAP program list.

    Program-LEVEL entries only (one per program, e.g. "Epilepsy Research Program
    (ERP) 2026"). The per-award announcements on each program page largely
    duplicate the CDMRP awards already arriving via Grants.gov, so we
    intentionally stay at the program level to avoid cross-source duplicates.
    Single request to Program.htm — polite and low-risk.
    """
    base = "https://ebrap.org/eBRAP/public/Program.htm"
    soup = _fetch_page(base)
    if soup is None:
        logger.info("eBRAP: program page fetch failed (unreachable or blocked)")
        return []

    grants = []
    seen_titles = set()
    total = 0
    for a in soup.find_all("a", href=True):
        if "ProgramFY.htm" not in a["href"]:
            continue
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        # Keep genuine program names: must contain "Program" or a parenthesised acronym
        if len(title) < 6:
            continue
        if "Program" not in title and not re.search(r"\([A-Z]{2,7}\)", title):
            continue
        if title in seen_titles:
            continue
        seen_titles.add(title)
        total += 1
        link = urljoin(base, a["href"])
        gid = _make_id("ebrap", title, link)
        if gid in seen_ids:
            continue
        grants.append(_make_grant(
            source="DoD CDMRP (eBRAP)", source_id="ebrap", title=title, link=link,
            synopsis=(f"U.S. Department of Defense CDMRP/MRDC research program listed on "
                      f"eBRAP: {title}. See the program page for current award mechanisms "
                      f"and application deadlines."),
            agency="Dept. of Defense -- CDMRP",
        ))
    if total:
        _mark_reached("ebrap", total)  # reached the list even if all programs already seen
    logger.info(f"eBRAP: {len(grants)} new program(s) (from {total} listed)")
    return grants


# ======================================================================
# SCRAPER REGISTRY
# ======================================================================

ALL_SCRAPERS = [
    # Tier A: RSS feeds (most reliable)
    ("nih_guide",                      scrape_nih_guide),
    ("nsf_funding",                    scrape_nsf_funding),
    ("pnd_rfps",                       scrape_pnd_rfps),
    # Tier B: API / structured
    ("arpa_h",                         scrape_arpa_h),
    ("dod_cdmrp",                      scrape_dod_cdmrp),
    ("ebrap",                          scrape_ebrap),   # DoD CDMRP/MRDC programs (program-level)
    # Tier C: Foundation HTML scrapers
    ("burroughs_wellcome",             scrape_burroughs_wellcome),
    ("hhmi",                           scrape_hhmi),
    ("aha",                            scrape_aha),
    ("acs",                            scrape_acs),
    ("damon_runyon",                   scrape_damon_runyon),
    ("alexs_lemonade",                 scrape_alexs_lemonade),
    ("doris_duke",                     scrape_doris_duke),
    ("rwjf",                           scrape_rwjf),
    ("keck",                           scrape_keck),
    ("beckman",                        scrape_beckman),
    ("march_of_dimes",                 scrape_march_of_dimes),
    ("alzheimers_assoc",               scrape_alzheimers_assoc),
    ("aacr",                           scrape_aacr),
    ("simons",                         scrape_simons),
    ("czi",                            scrape_czi),
    ("gates",                          scrape_gates),
    ("wellcome_trust",                 scrape_wellcome_trust),
    ("koch_foundation",                scrape_koch_foundation),
    ("yield_giving",                   scrape_yield_giving),
    # Tier C: Portals
    ("proposal_central",               scrape_proposal_central),
    ("science_philanthropy_alliance",  scrape_science_philanthropy_alliance),
    ("pcori",                          scrape_pcori),
    # Tier C: Med-ed / physician-focused foundations (2026-06-04)
    ("ama",                            scrape_ama),
    ("gold_foundation",                scrape_gold_foundation),
    ("macy_foundation",                scrape_macy_foundation),
    # Tier D: Med school pages
    ("stanford_rmg",                   scrape_stanford_rmg),
    ("ucla_dgsom",                     scrape_ucla_dgsom),
    ("miami_miller",                   scrape_miami_miller),
    ("aamc_grants",                    scrape_aamc_grants),
]


# ======================================================================
# SCRAPER HEALTH TRACKING
# ======================================================================

_last_scraper_health = {}

# Reachability map for the current run: source key -> number of raw items the
# scraper successfully fetched from its feed/page (before seen/junk filtering).
# A source that reaches its feed but yields 0 *new* grants (all already seen, or
# only admin notices) is "quiet", NOT broken — this lets the health logic tell
# the two apart instead of flagging a healthy-but-quiet feed as "likely_broken".
_reached_this_run: dict = {}


def _mark_reached(key: str, n_items: int):
    """Record that `key`'s source was successfully fetched this run (n_items raw)."""
    _reached_this_run[key] = n_items


def get_last_scraper_health():
    return _last_scraper_health


def _load_scraper_health(path="data/scraper_health.json"):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_scraper_health(health, path="data/scraper_health.json"):
    try:
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(health, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save scraper health: {e}")


def fetch_all_external_grants(seen_ids, config):
    """
    Run all enabled external source scrapers.
    Returns a combined list of normalised grant dicts.
    """
    global _last_scraper_health

    ext_cfg = config.get("external_sources", {})
    enabled_sources = ext_cfg.get("enabled_sources", None)
    disabled_sources = set(ext_cfg.get("disabled_sources", []))

    _reached_this_run.clear()  # reset per-run reachability before scrapers run
    health = _load_scraper_health()
    all_grants = []
    sources_tried = 0
    sources_succeeded = 0
    per_source_results = {}    # for diagnostic reporting

    for key, scraper_fn in ALL_SCRAPERS:
        if key in disabled_sources:
            continue
        if enabled_sources is not None and key not in enabled_sources:
            continue

        sources_tried += 1
        if key not in health:
            health[key] = {
                "consecutive_zeros": 0,
                "last_success": None,
                "total_found": 0,
                "last_status": None,
                "last_error": None,
            }

        try:
            grants = scraper_fn(seen_ids)
            # Central junk gate: drop navigation/section/UI "grants" that the
            # broad link scanners pick up (e.g. "View research…", "RFA Details",
            # "Funding Opportunities"), regardless of which scraper produced them.
            raw_count = len(grants)
            grants = [g for g in grants if not _is_junk_title(g.get("title", ""))]
            dropped = raw_count - len(grants)
            if dropped:
                logger.info(f"  '{key}': dropped {dropped} junk/nav title(s), kept {len(grants)}")
            all_grants.extend(grants)
            sources_succeeded += 1
            per_source_results[key] = len(grants)
            health[key]["last_error"] = None

            if len(grants) > 0:
                health[key]["consecutive_zeros"] = 0
                health[key]["last_success"] = datetime.now().isoformat()
                health[key]["total_found"] = health[key].get("total_found", 0) + len(grants)
                health[key]["last_status"] = "new"
            elif _reached_this_run.get(key, 0) > 0:
                # Source was reachable (we fetched feed items) but nothing was new
                # this run — all already seen, or only admin notices. This is a
                # healthy "quiet" source, so record reachability and clear the
                # zero-streak so it can never be mislabeled "likely_broken".
                health[key]["consecutive_zeros"] = 0
                health[key]["last_reachable"] = datetime.now().isoformat()
                health[key]["last_status"] = "quiet"
            else:
                health[key]["consecutive_zeros"] = health[key].get("consecutive_zeros", 0) + 1
                # Distinguish a quiet-but-working source (has produced or been
                # reachable before) from one that has NEVER yielded anything and
                # was not reachable this run (likely silently broken).
                healthy_before = health[key].get("last_success") or health[key].get("last_reachable")
                health[key]["last_status"] = "quiet" if healthy_before else "no_results"
                if health[key]["last_status"] == "no_results" and health[key]["consecutive_zeros"] >= 3:
                    logger.warning(
                        f"SCRAPER ALERT: '{key}' has NEVER returned a result "
                        f"({health[key]['consecutive_zeros']} runs) — likely broken."
                    )
        except Exception as e:
            logger.error(f"Scraper '{key}' failed: {e}", exc_info=True)
            per_source_results[key] = -1  # -1 = error
            health[key]["last_status"] = "error"
            health[key]["last_error"] = str(e)[:200]

        time.sleep(REQUEST_DELAY)

    _save_scraper_health(health)

    # Build health alerts — only for sources actually tried this run.
    #   "error"         → the scraper raised an exception
    #   "likely_broken" → has never returned a result in 3+ consecutive runs AND
    #                     was never even reachable (now correctly catches JS-shell
    #                     scrapers, since reachability is only marked when real
    #                     grant-like links are found — see _scrape_generic)
    #   "quiet_stale"   → reachable/healthy in the past, but no NEW grant in
    #                     quiet_stale_days (advisory: link patterns may have gone
    #                     stale, or the source is genuinely dormant — worth a look)
    # A "quiet" source that produced within the window is NOT alerted — that's the
    # normal state for sources listing a stable set of open RFPs.
    stale_days = config.get("external_sources", {}).get("quiet_stale_days", 60)

    def _days_since(iso_ts):
        """Whole days since an ISO timestamp; None if unparseable/missing."""
        if not iso_ts:
            return None
        try:
            return (datetime.now() - datetime.fromisoformat(iso_ts)).days
        except (ValueError, TypeError):
            return None

    health_alerts = []
    for k in per_source_results:
        v = health.get(k, {})
        zeros = v.get("consecutive_zeros", 0)
        if v.get("last_status") == "error":
            health_alerts.append({
                "source": k, "status": "error", "consecutive_zeros": zeros,
                "last_success": v.get("last_success") or "never",
                "detail": v.get("last_error") or "exception raised",
            })
        elif v.get("last_success") is None and zeros >= 3:
            # Never yielded a grant AND not reachable-with-real-content for 3+
            # consecutive runs (consecutive_zeros only climbs on not-reached runs,
            # so zeros>=3 already means "not reachable recently" — we deliberately
            # do NOT gate on `last_reachable is None`, because a stale last_reachable
            # timestamp persisted from before the reachability fix would otherwise
            # suppress this alert forever).
            health_alerts.append({
                "source": k, "status": "likely_broken", "consecutive_zeros": zeros,
                "last_success": "never",
                "detail": (f"no grant-like content found in {zeros} consecutive runs "
                           f"and never yielded a result — page likely JS-rendered or "
                           f"its structure changed (scraper gets an empty shell)"),
            })
        else:
            # Advisory: was healthy before but has gone quiet for too long.
            since_success = _days_since(v.get("last_success"))
            if since_success is not None and since_success >= stale_days:
                health_alerts.append({
                    "source": k, "status": "quiet_stale", "consecutive_zeros": zeros,
                    "last_success": v.get("last_success"),
                    "detail": (f"no new grant in {since_success} days "
                               f"(threshold {stale_days}) — verify link patterns "
                               f"still match the source's current page structure"),
                })

    # Per-source health state, surfaced in the diagnostic even when NOT alerting.
    # Without this, consecutive_zeros/last_success/last_reachable were only visible
    # inside an active alert entry, so a source silently climbing toward
    # likely_broken (or a quiet-but-healthy source) was invisible in the daily
    # diagnostic — you had to read scraper_health.json off the container to tell
    # whether the health tracker was advancing. This block closes that gap.
    per_source_health = {
        k: {
            "new_this_run": per_source_results.get(k),   # grants kept this run (-1 = error)
            "consecutive_zeros": health.get(k, {}).get("consecutive_zeros", 0),
            "last_status": health.get(k, {}).get("last_status"),
            "last_success": health.get(k, {}).get("last_success"),
            "last_reachable": health.get(k, {}).get("last_reachable"),
            "reached_this_run": _reached_this_run.get(k, 0),
            "total_found": health.get(k, {}).get("total_found", 0),
        }
        for k in per_source_results
    }

    # Build diagnostic summary in the format emailer.py expects
    _last_scraper_health = {
        "per_source": per_source_results,
        "per_source_health": per_source_health,
        "sources_tried": sources_tried,
        "sources_succeeded": sources_succeeded,
        "total_new_grants": len(all_grants),
        "health_alerts": health_alerts,
    }

    logger.info(
        f"External sources complete: {sources_succeeded}/{sources_tried} succeeded, "
        f"{len(all_grants)} total new grants found"
    )
    return all_grants
