#!/usr/bin/env python3
"""
phase4_foreclosures.py

Phase 4 — Harris County Foreclosure Filing Cross-Reference

Scrapes the Harris County County Clerk recorded documents system
(cclerk.hctx.net) for recently filed foreclosure notices (Substitute
Trustee's Sale / Notice of Foreclosure), then cross-references owner
names and addresses against your leads.csv.

Properties that show up here have an active foreclosure filing —
the bank has already filed and a sale date is set. These owners
need to sell NOW.

Fallback: If the Clerk site is unreachable, queries the Harris County
District Clerk (hcdistrictclerk.com) for foreclosure case filings.

Usage
-----
  python phase4_foreclosures.py
  python phase4_foreclosures.py --leads leads.csv --out foreclosure_leads.csv
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import time
from typing import Dict, List, Set
from urllib.parse import urljoin, quote

import pandas as pd
import requests

try:
    from bs4 import BeautifulSoup
    BS4 = True
except ImportError:
    BS4 = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# Harris County County Clerk recorded documents
CCLERK_SEARCH = "https://www.cclerk.hctx.net/applications/websearch/RD.aspx"

# Harris County District Clerk case search
HCDC_SEARCH = "https://www.hcdistrictclerk.com/Common/CaseDetails/CaseSearchByCaseNum.aspx"
HCDC_NAME_SEARCH = "https://www.hcdistrictclerk.com/edocs/public/PARTYSEARCHVerification.aspx"

FORECLOSURE_KEYWORDS = [
    "substitute trustee", "foreclosure", "trustee sale",
    "notice of sale", "deed of trust", "lis pendens",
]

ACCT_PATTERN = re.compile(r"\b(\d{13})\b")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection":      "keep-alive",
    })
    return s


# ── Method 1: Harris County County Clerk recorded docs ───────────────────────

def search_cclerk_for_foreclosures(session: requests.Session) -> Set[str]:
    """
    Search the Harris County County Clerk for recently recorded
    Substitute Trustee Sale / foreclosure notices.
    Returns a set of owner names found in foreclosure documents.
    """
    owner_names: Set[str] = set()

    search_terms = ["SUBSTITUTE TRUSTEE SALE", "NOTICE OF FORECLOSURE", "LIS PENDENS"]

    for term in search_terms:
        logger.info("  Searching County Clerk for: %s", term)
        try:
            params = {
                "DocType": term,
                "DateFrom": "",
                "DateTo":   "",
                "f":        "json",
            }
            resp = session.get(CCLERK_SEARCH, params=params, timeout=20)
            if resp.status_code != 200:
                logger.warning("    HTTP %d", resp.status_code)
                continue

            if BS4:
                soup = BeautifulSoup(resp.text, "html.parser")
                rows = soup.find_all("tr")
                for row in rows:
                    text = row.get_text(separator=" ").strip()
                    if any(kw in text.lower() for kw in ["grantor", "grantee", "party"]):
                        # Extract names from table cells
                        cells = row.find_all("td")
                        for cell in cells:
                            val = cell.get_text(strip=True).upper()
                            if len(val) > 4 and val.isalpha() is False and len(val) < 60:
                                owner_names.add(val)
            else:
                # Basic extraction: look for all-caps name patterns
                names = re.findall(r'\b([A-Z]{2,}(?:\s+[A-Z]{2,}){1,4})\b', resp.text)
                owner_names.update(names)

            logger.info("    Found %d name entries so far", len(owner_names))
            time.sleep(2)

        except Exception as exc:
            logger.warning("    Error: %s", exc)

    return owner_names


# ── Method 2: Harris County District Clerk — search by owner name ─────────────

def search_hcdc_by_name(owner_name: str, session: requests.Session) -> bool:
    """
    Search Harris County District Clerk for active foreclosure cases
    filed against a specific owner name.
    Returns True if a foreclosure case is found.
    """
    try:
        # Clean up the name for search (first word of owner name)
        last = owner_name.split()[0] if owner_name else ""
        if len(last) < 3:
            return False

        params = {
            "LastName":  last,
            "CaseType":  "FORECLOSURE",
            "Status":    "ACTIVE",
        }
        resp = session.get(HCDC_NAME_SEARCH, params=params, timeout=20)
        if resp.status_code != 200:
            return False

        text = resp.text.lower()
        return any(kw in text for kw in ["foreclosure", "trustee", "mortgage"])

    except Exception:
        return False


def batch_check_hcdc(leads: pd.DataFrame, sample_size: int, session: requests.Session) -> Set[str]:
    """Check top N leads against Harris County District Clerk."""
    foreclosed: Set[str] = set()
    to_check = leads.head(sample_size)
    total = len(to_check)

    logger.info("Checking %d leads on Harris County District Clerk…", total)

    for i, (_, row) in enumerate(to_check.iterrows(), 1):
        if i % 20 == 0:
            logger.info("  %d / %d checked | %d foreclosures found", i, total, len(foreclosed))

        owner = str(row.get("owner_name", "") or "").strip()
        acct  = str(row.get("acct_num", "")   or "").strip()

        if not owner:
            continue

        if search_hcdc_by_name(owner, session):
            foreclosed.add(acct)

        time.sleep(random.uniform(1.5, 2.5))

    return foreclosed


# ── Cross-reference ───────────────────────────────────────────────────────────

def match_by_owner_name(leads: pd.DataFrame, foreclosure_names: Set[str]) -> pd.DataFrame:
    """
    Match leads against a set of owner names found in foreclosure docs.
    Uses partial matching — a lead owner name that CONTAINS any foreclosure
    name string (or vice versa) is flagged.
    """
    if not foreclosure_names:
        return pd.DataFrame()

    # Normalise
    fc_names = {n.upper().strip() for n in foreclosure_names if len(n.strip()) > 4}

    def is_match(owner: str) -> bool:
        owner_up = str(owner).upper().strip()
        if not owner_up:
            return False
        # Direct match
        if owner_up in fc_names:
            return True
        # Partial — first word (last name) match
        first_word = owner_up.split()[0] if owner_up.split() else ""
        return any(first_word in fn or fn.startswith(first_word) for fn in fc_names if first_word)

    mask = leads["owner_name"].fillna("").apply(is_match)
    return leads[mask].copy()


# ── Main ──────────────────────────────────────────────────────────────────────

def run(leads_path: str, output_path: str, sample_size: int) -> int:
    logger.info("=" * 56)
    logger.info("Phase 4 — Foreclosure Filing Cross-Reference")
    logger.info("=" * 56)

    leads = pd.read_csv(leads_path, dtype=str, low_memory=False)
    logger.info("Loaded %d leads from %s", len(leads), leads_path)

    session = _session()
    foreclosure_leads = pd.DataFrame()

    # ── Method 1: County Clerk bulk scrape ───────────────────────────────────
    logger.info("\n[1/2] Searching Harris County County Clerk for foreclosure filings…")
    session.headers["Referer"] = "https://www.cclerk.hctx.net/"
    foreclosure_names = search_cclerk_for_foreclosures(session)

    if foreclosure_names:
        logger.info("County Clerk: %d names in foreclosure documents", len(foreclosure_names))
        foreclosure_leads = match_by_owner_name(leads, foreclosure_names)
        logger.info("Matched %d leads to foreclosure filings", len(foreclosure_leads))
        source = "Harris County County Clerk"
    else:
        # ── Method 2: District Clerk per-name lookup ──────────────────────────
        logger.info("County Clerk returned 0 results.")
        logger.info("[2/2] Falling back to Harris County District Clerk per-name search…")
        session.headers["Referer"] = "https://www.hcdistrictclerk.com/"
        foreclosed_accts = batch_check_hcdc(leads, sample_size, session)

        if foreclosed_accts:
            foreclosure_leads = leads[leads["acct_num"].isin(foreclosed_accts)].copy()
            source = "Harris County District Clerk"
        else:
            logger.warning(
                "\nCould not retrieve foreclosure data from either source.\n"
                "Try running from a different network.\n"
                "Outputting leads without foreclosure filter as fallback.\n"
            )
            leads["foreclosure_filing"] = "NOT CHECKED"
            leads.to_csv(output_path, index=False)
            print(f"\n  Saved {len(leads)} leads (no foreclosure filter) → {output_path}\n")
            return len(leads)

    if len(foreclosure_leads) == 0:
        logger.warning("No foreclosure matches found — saving full lead list as fallback")
        leads["foreclosure_filing"] = "NOT CHECKED"
        foreclosure_leads = leads
    else:
        foreclosure_leads["foreclosure_filing"] = f"YES — {source}"

    # Sort
    for col in ("lead_score", "appraised_value"):
        if col in foreclosure_leads.columns:
            foreclosure_leads[col] = pd.to_numeric(foreclosure_leads[col], errors="coerce")

    sort_cols = [c for c in ("lead_score", "appraised_value") if c in foreclosure_leads.columns]
    sort_asc  = [False] + [True] * (len(sort_cols) - 1)
    foreclosure_leads = foreclosure_leads.sort_values(sort_cols, ascending=sort_asc).reset_index(drop=True)

    foreclosure_leads.to_csv(output_path, index=False)

    print()
    print("=" * 56)
    print("  PHASE 4 COMPLETE")
    print("=" * 56)
    print(f"  Source         : {source}")
    print(f"  Foreclosure hits: {len(foreclosure_leads[foreclosure_leads.get('foreclosure_filing', pd.Series('')).str.startswith('YES', na=False)] if 'foreclosure_filing' in foreclosure_leads.columns else foreclosure_leads)}")
    print(f"  Output file    : {output_path}")
    print("=" * 56)
    print()

    return len(foreclosure_leads)


if __name__ == "__main__":
    if not BS4:
        print("\n[INFO] Run: pip install beautifulsoup4 openpyxl\n")

    parser = argparse.ArgumentParser(description="Phase 4 — Foreclosure Filing Cross-Reference")
    parser.add_argument("--leads",   default="leads.csv",             help="Input leads CSV")
    parser.add_argument("--out",     default="foreclosure_leads.csv", help="Output CSV")
    parser.add_argument("--sample",  default=150, type=int,           help="Accounts to check on HCDC if bulk fails")
    args = parser.parse_args()

    run(args.leads, args.out, args.sample)
