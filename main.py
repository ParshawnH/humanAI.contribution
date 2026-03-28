#!/usr/bin/env python3
"""
Minimal FOA ingestion pipeline (screening task).

Takes a Grants.gov or NSF URL, pulls the opportunity data via their public APIs,
normalizes it into a common schema, slaps on some keyword-based semantic tags,
and dumps foa.json + foa.csv.

Usage:
    python main.py --url "https://www.grants.gov/search-results-detail/349644" --out_dir ./out
    python main.py --url "https://www.nsf.gov/awardsearch/showAward?AWD_ID=2401234" --out_dir ./out
"""

import argparse
import csv
import hashlib
import json
import os
import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup


# API endpoints — neither requires auth, which is nice
GRANTS_GOV_FETCH = "https://api.grants.gov/v1/api/fetchOpportunity"
NSF_AWARDS_API = "https://www.research.gov/awardapi-service/v1/awards.json"

UA = "FOA-Pipeline/1.0 (screening task)"


# ── Keyword ontology for tagging ──────────────────────────────────────────
#
# This is deliberately simple: just substring matching against the FOA text.
# A real system would use embeddings or at least stemming, but for a 2-4 hour
# task, keyword lists get us pretty far. I used partial stems where it made
# sense (e.g. "epidemiolog" catches epidemiology/epidemiological/etc).

ONTOLOGY = {
    "research_domains": {
        "artificial intelligence": [
            "artificial intelligence", "ai ", "machine learning", "deep learning",
            "neural network", "nlp", "natural language processing", "computer vision",
            "generative ai", "large language model",
        ],
        "biology": [
            "biology", "biological", "genomic", "molecular", "cell ",
            "ecology", "evolution", "microbi", "biodiversity", "bioinformatics",
        ],
        "biomedical sciences": [
            "biomedical", "clinical trial", "disease", "therapeutic", "pathology",
            "pharmacolog", "epidemiolog", "oncolog", "cardio", "neurolog",
            "immunolog", "public health", "health disparit",
        ],
        "chemistry": [
            "chemistry", "chemical", "catalys", "polymer", "electrochemi",
            "spectroscop",
        ],
        "climate & environment": [
            "climate", "environment", "sustainability", "carbon",
            "renewable energy", "pollution", "conservation", "ecosystem",
            "greenhouse", "ocean", "atmospheric", "geosci",
        ],
        "computer science": [
            "computer science", "software", "algorithm", "cybersecurity",
            "distributed system", "database", "cloud computing", "data science",
            "high-performance computing",
        ],
        "education": [
            "education", "stem education", "curriculum", "pedagog",
            "student learning", "k-12", "undergraduate", "broadening participation",
            "training program",
        ],
        "engineering": [
            "engineering", "robotics", "manufacturing", "materials",
            "nanotechnolog", "bioengineering", "mechanical", "electrical",
            "aerospace",
        ],
        "mathematics & statistics": [
            "mathematic", "statistic", "stochastic", "optimization",
            "topology", "algebra", "probability", "numerical method",
        ],
        "physics & astronomy": [
            "physics", "quantum", "astrophysic", "cosmolog", "particle physics",
            "optic", "photon", "plasma", "condensed matter",
        ],
        "social sciences": [
            "social science", "sociolog", "psycholog", "anthropolog",
            "economics", "political science", "demograph", "behavioral",
            "cognitive science", "communit",
        ],
    },
    "methods_approaches": {
        "computational modeling": [
            "computational model", "simulation", "finite element",
            "agent-based model", "numerical simulation", "monte carlo",
        ],
        "data analytics": [
            "data analy", "big data", "data mining", "data-driven",
            "predictive model",
        ],
        "experimental research": [
            "experiment", "laboratory", "field study", "controlled trial",
            "bench research", "randomized",
        ],
        "field research": [
            "field research", "field work", "fieldwork", "in situ",
            "observational study",
        ],
        "machine learning": [
            "machine learning", "deep learning", "neural network",
            "reinforcement learning", "supervised learning", "unsupervised",
            "transfer learning", "classification", "regression model",
        ],
        "mixed methods": [
            "mixed method", "qualitative and quantitative", "triangulat",
        ],
        "survey & qualitative": [
            "survey", "interview", "focus group", "ethnograph", "qualitative",
            "case study", "content analysis", "grounded theory",
        ],
    },
    "populations": {
        "children & youth": [
            "children", "youth", "adolescent", "pediatric", "juvenile",
            "k-12 student", "young people",
        ],
        "communities of color": [
            "minority", "underrepresented", "communities of color",
            "african american", "hispanic", "latino", "latina",
            "indigenous", "native american", "tribal", "alaska native",
        ],
        "elderly": ["elderly", "older adult", "aging", "geriatric"],
        "general population": [
            "general population", "public", "citizen", "nationwide",
        ],
        "patients & clinical": [
            "patient", "clinical population", "cohort study", "human subject",
        ],
        "rural communities": [
            "rural", "remote communit", "underserved area",
        ],
        "veterans": [
            "veteran", "military", "service member", "armed forces",
        ],
        "women & gender minorities": [
            "women", "gender", "female", "maternal", "gender minorit", "lgbtq",
        ],
    },
    "sponsor_themes": {
        "capacity building": [
            "capacity building", "infrastructure", "core facilit",
            "institutional", "research capacity", "instrumentation",
        ],
        "defense & national security": [
            "defense", "national security", "military", "homeland security",
            "dod ", "darpa",
        ],
        "economic development": [
            "economic development", "innovation", "entrepreneurship",
            "small business", "sbir", "sttr", "commercialization",
            "technology transfer",
        ],
        "energy": [
            "energy", "renewable", "solar", "wind", "nuclear", "fossil",
            "battery", "grid", "fuel cell", "biofuel", "hydrogen",
        ],
        "health & well-being": [
            "health", "well-being", "wellness", "mental health",
            "disease prevention", "drug", "therapeutic", "vaccine", "nutrition",
        ],
        "international collaboration": [
            "international", "global", "bilateral", "multilateral",
            "cross-border",
        ],
        "workforce development": [
            "workforce", "training", "professional development", "career",
            "mentor", "fellowship", "postdoctoral",
        ],
    },
}


# ── URL parsing ───────────────────────────────────────────────────────────

def figure_out_source(url):
    """Look at the URL and figure out if it's Grants.gov or NSF, plus the ID."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path

    # Grants.gov style: /search-results-detail/349644
    m = re.search(r"/search-results-detail/(\d+)", path)
    if "grants.gov" in host and m:
        return "grants.gov", m.group(1)

    # NSF award page: showAward?AWD_ID=...
    if "nsf.gov" in host and "showAward" in path:
        qs = parse_qs(parsed.query)
        awd_id = qs.get("AWD_ID", qs.get("awd_id", [None]))[0]
        if awd_id:
            return "nsf", awd_id

    # Direct NSF API link: ...awards.json?id=...
    if "research.gov" in host and "awards" in path:
        qs = parse_qs(parsed.query)
        awd_id = qs.get("id", [None])[0]
        if awd_id:
            return "nsf", awd_id

    raise ValueError(
        f"Can't figure out what to do with this URL: {url}\n"
        "I understand these formats:\n"
        "  https://www.grants.gov/search-results-detail/<OPP_ID>\n"
        "  https://www.nsf.gov/awardsearch/showAward?AWD_ID=<ID>"
    )


# ── Date helpers ──────────────────────────────────────────────────────────
# Grants.gov dates are a mess — sometimes "Oct 11, 2023 12:00:00 AM EDT",
# sometimes just a date string. NSF uses MM/DD/YYYY. We try a few formats
# and return ISO or None.

def parse_grants_date(raw):
    if not raw:
        return None
    # strip timezone suffix if present (EDT, EST, etc) since strptime chokes on it
    cleaned = re.sub(r'\s+(EDT|EST|CDT|CST|PDT|PST|UTC)$', '', raw.strip())
    for fmt in ("%b %d, %Y %I:%M:%S %p", "%b %d, %Y", "%Y-%m-%d-%H-%M-%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return raw.strip()  # give up, return as-is


def parse_nsf_date(raw):
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return raw.strip()


# ── Small helpers ─────────────────────────────────────────────────────────

def format_award_range(floor_val, ceil_val):
    """Turn the floor/ceiling values into something readable like '$50,000 – $500,000'."""
    def to_int(v):
        if v is None:
            return None
        try:
            return int(float(str(v).replace(",", "").replace("$", "").strip()))
        except (ValueError, TypeError):
            return None

    lo, hi = to_int(floor_val), to_int(ceil_val)
    if lo and hi:
        return f"${lo:,} – ${hi:,}"
    if hi:
        return f"Up to ${hi:,}"
    if lo:
        return f"From ${lo:,}"
    return None


def strip_html(text):
    """Remove HTML tags from a string, if any are present."""
    if not text or "<" not in text:
        return text or ""
    return BeautifulSoup(text, "html.parser").get_text(separator=" ").strip()


# ── Grants.gov ingestion ─────────────────────────────────────────────────

def fetch_grants_gov(opp_id, url):
    """Pull an opportunity from the Grants.gov REST API and normalize it."""
    print(f"  Fetching Grants.gov opportunity {opp_id} via API...")

    resp = requests.post(
        GRANTS_GOV_FETCH,
        json={"opportunityId": int(opp_id)},
        headers={"Content-Type": "application/json", "User-Agent": UA},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("errorcode") and payload["errorcode"] != 0:
        raise RuntimeError(f"API error: {payload.get('msg', '?')}")

    data = payload.get("data", {})
    syn = data.get("synopsis", {})
    fcast = data.get("forecast", {})

    # description can be in synopsis or forecast, and might contain HTML
    desc = strip_html(syn.get("synopsisDesc") or fcast.get("forecastDesc") or "")

    # eligibility: the API gives us a list of applicant type objects
    applicant_types = syn.get("applicantTypes", [])
    eligibility = "; ".join(t.get("description", "") for t in applicant_types)

    return {
        "foa_id": data.get("opportunityNumber") or f"GRANTS-{opp_id}",
        "title": data.get("opportunityTitle", ""),
        "agency": syn.get("agencyName") or data.get("owningAgencyCode", ""),
        "open_date": parse_grants_date(syn.get("postingDate") or fcast.get("estimatedPostDate")),
        "close_date": parse_grants_date(
            syn.get("responseDate") or syn.get("responseDateDesc")
            or fcast.get("estimatedApplicationDueDate")
        ),
        "eligibility": eligibility,
        "description": desc,
        "award_range": format_award_range(
            syn.get("awardFloor") or fcast.get("awardFloor"),
            syn.get("awardCeiling") or fcast.get("awardCeiling"),
        ),
        "source_url": url,
        "source": "grants.gov",
        "ingested_at": datetime.utcnow().isoformat() + "Z",
    }


# ── NSF ingestion ────────────────────────────────────────────────────────

def fetch_nsf(award_id, url):
    """Pull an NSF award from the Research.gov API and normalize it."""
    print(f"  Fetching NSF award {award_id} via Research.gov API...")

    resp = requests.get(
        NSF_AWARDS_API,
        params={
            "id": award_id,
            "printFields": ",".join([
                "id", "title", "agency", "fundsObligatedAmt",
                "awardeeName", "awardeeCity", "awardeeStateCode",
                "date", "startDate", "expDate", "abstractText",
                "pdPIName", "coPDPI", "program",
            ]),
        },
        headers={"User-Agent": UA},
        timeout=30,
    )
    resp.raise_for_status()
    awards = resp.json().get("response", {}).get("award", [])

    if not awards:
        raise RuntimeError(f"No award found with ID {award_id}")

    a = awards[0]
    funds = a.get("fundsObligatedAmt")

    return {
        "foa_id": f"NSF-{a.get('id', award_id)}",
        "title": a.get("title", ""),
        "agency": a.get("agency", "NSF"),
        "open_date": parse_nsf_date(a.get("startDate") or a.get("date")),
        "close_date": parse_nsf_date(a.get("expDate")),
        "eligibility": "",  # awards API doesn't include this
        "description": strip_html(a.get("abstractText", "")),
        "award_range": f"${int(funds):,}" if funds else None,
        "source_url": url,
        "source": "nsf",
        "ingested_at": datetime.utcnow().isoformat() + "Z",
    }


# ── HTML fallbacks ───────────────────────────────────────────────────────
# If the APIs are down or blocked, we can try scraping the web page directly.
# These won't get as much structured data but it's better than nothing.

def scrape_grants_gov(opp_id, url):
    print(f"  API didn't work, trying to scrape the HTML page...")
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(separator="\n")

    # try to pull a title from the page — these selectors might break
    title_el = soup.find("h1") or soup.find("h2")
    title = title_el.get_text(strip=True) if title_el else f"Opportunity {opp_id}"

    return {
        "foa_id": f"GRANTS-{opp_id}",
        "title": title,
        "agency": "",
        "open_date": None,
        "close_date": None,
        "eligibility": "",
        "description": text[:3000],  # just grab whatever we can
        "award_range": None,
        "source_url": url,
        "source": "grants.gov",
        "ingested_at": datetime.utcnow().isoformat() + "Z",
    }


def scrape_nsf(award_id, url):
    print(f"  API didn't work, trying to scrape the HTML page...")
    page_url = f"https://www.nsf.gov/awardsearch/showAward?AWD_ID={award_id}"
    resp = requests.get(page_url, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    title_el = soup.find("span", class_="award-title-text") or soup.find("h3")
    title = title_el.get_text(strip=True) if title_el else f"Award {award_id}"

    return {
        "foa_id": f"NSF-{award_id}",
        "title": title,
        "agency": "National Science Foundation",
        "open_date": None,
        "close_date": None,
        "eligibility": "",
        "description": soup.get_text(separator=" ")[:3000],
        "award_range": None,
        "source_url": url,
        "source": "nsf",
        "ingested_at": datetime.utcnow().isoformat() + "Z",
    }


# ── Semantic tagging ─────────────────────────────────────────────────────

def apply_tags(foa):
    """
    Run through our ontology and tag the FOA based on keyword matches.

    Nothing fancy — we just concatenate the title + description + eligibility,
    lowercase everything, and check if any of our keywords appear as substrings.
    One keyword match is enough to assign a label.
    """
    blob = " ".join([
        foa.get("title") or "",
        foa.get("description") or "",
        foa.get("eligibility") or "",
    ]).lower()

    tags = {}
    for category, labels in ONTOLOGY.items():
        hits = []
        for label, keywords in labels.items():
            if any(kw in blob for kw in keywords):
                hits.append(label)
        tags[category] = sorted(set(hits))

    return tags


# ── Export ────────────────────────────────────────────────────────────────

CSV_COLUMNS = [
    "foa_id", "title", "agency", "open_date", "close_date",
    "eligibility", "description", "award_range", "source_url", "source",
    "ingested_at",
    "tags_research_domains", "tags_methods_approaches",
    "tags_populations", "tags_sponsor_themes",
]


def save_json(foa, tags, out_dir):
    record = {**foa}
    for k, v in tags.items():
        record[f"tags_{k}"] = v
    path = os.path.join(out_dir, "foa.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
    print(f"  Wrote {path}")
    return path


def save_csv(foa, tags, out_dir):
    row = {**foa}
    for k, v in tags.items():
        row[f"tags_{k}"] = "; ".join(v) if v else ""
    path = os.path.join(out_dir, "foa.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})
    print(f"  Wrote {path}")
    return path


# ── Main pipeline ─────────────────────────────────────────────────────────

def ingest(url):
    """Figure out the source and fetch + normalize the FOA."""
    source, opp_id = figure_out_source(url)

    if source == "grants.gov":
        try:
            return fetch_grants_gov(opp_id, url)
        except Exception as e:
            print(f"  Grants.gov API failed: {e}")
            return scrape_grants_gov(opp_id, url)
    else:
        try:
            return fetch_nsf(opp_id, url)
        except Exception as e:
            print(f"  NSF API failed: {e}")
            return scrape_nsf(opp_id, url)


def run(url, out_dir):
    os.makedirs(out_dir, exist_ok=True)

    print(f"\n{'=' * 56}")
    print(f"  FOA Ingestion Pipeline")
    print(f"{'=' * 56}")
    print(f"  URL:    {url}")
    print(f"  Output: {out_dir}/")
    print(f"{'=' * 56}\n")

    # 1) fetch and normalize
    foa = ingest(url)
    print(f"\n  FOA ID:  {foa['foa_id']}")
    print(f"  Title:   {foa['title'][:72]}{'...' if len(foa.get('title','')) > 72 else ''}")
    print(f"  Agency:  {foa['agency']}")

    # 2) tag it
    tags = apply_tags(foa)
    any_tags = False
    for cat, labels in tags.items():
        if labels:
            any_tags = True
            print(f"  [{cat}] {', '.join(labels)}")
    if not any_tags:
        print("  (no tags matched — might need to expand the ontology)")

    # 3) export
    print()
    save_json(foa, tags, out_dir)
    save_csv(foa, tags, out_dir)

    print(f"\n  Done!\n")


# ── CLI ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest a single FOA, extract fields, tag it, and export to JSON + CSV."
    )
    parser.add_argument("--url", required=True, help="Grants.gov or NSF opportunity URL")
    parser.add_argument("--out_dir", default="./out", help="Where to write foa.json and foa.csv")
    args = parser.parse_args()

    run(args.url, args.out_dir)
