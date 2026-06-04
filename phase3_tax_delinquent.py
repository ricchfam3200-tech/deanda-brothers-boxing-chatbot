#!/usr/bin/env python3
"""
phase3_tax_delinquent.py

Phase 3 — Harris County Tax Delinquent Cross-Reference

Pulls the Harris County upcoming tax sale list from LGBS
(Linebarger Goggan Blair & Sampson) and cross-references it against
your leads.csv to produce priority_leads.csv.

How it works
------------
1. Scrapes lgbs.com for the Harris County tax sale list
   — tries HTML tables, Excel downloads, and direct account number extraction
2. If LGBS is unreachable, falls back to checking hctax.net per-account
3. Cross-references delinquent accounts against your leads.csv
4. Outputs priority_leads.csv sorted by lead_score (best first)

Usage
-----
  python phase3_tax_delinquent.py
  python phase3_tax_delinquent.py --leads leads.csv --out priority_leads.csv
  python phase3_tax_delinquent.py --leads leads.csv --out priority_leads.csv --hctax-sample 500
"""

from __future__ import annotations

import argparse
import io
import logging
import random
import re
import time
from typing import Optional, Set
from urllib.parse import urljoin

import pandas as pd
import requests

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# HCAD account numbers = 13 digits
ACCT_PATTERN = re.compile(r"\b(\d{13})\b")

LGBS_ROOTS = [
    "https://lgbs.com",
    "https://www.lgbs.com",
]

HARRIS_KEYWORDS = ["harris", "harris county"]

HCTAX_URL = "https://www.hctax.net/Property/PropertyTaxes"


# ── Session ───────────────────────────────────────────────────────────────────

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection":      "keep-alive",
        "Referer":         "https://lgbs.com/",
    })
    return s


# ── LGBS Scraper ──────────────────────────────────────────────────────────────

def _extract_accounts_from_text(text: str) -> Set[str]:
    return set(ACCT_PATTERN.findall(text))


def _find_harris_links(html: str, base_url: str) -> list:
    """Find all links on a page that likely point to Harris County sale data."""
    if not BS4_AVAILABLE:
        # Fallback: simple regex link extraction
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE)
        return [
            urljoin(base_url, h) for h in hrefs
            if any(kw in h.lower() for kw in HARRIS_KEYWORDS)
        ]
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        text = tag.get_text(strip=True).lower()
        if any(kw in href.lower() or kw in text for kw in HARRIS_KEYWORDS):
            links.append(urljoin(base_url, href))
    return links


def _parse_excel_accounts(content: bytes) -> Set[str]:
    """Try to read an Excel file and extract 13-digit account numbers."""
    try:
        df = pd.read_excel(io.BytesIO(content), dtype=str, engine="openpyxl")
        text = df.to_string()
        return _extract_accounts_from_text(text)
    except Exception as exc:
        logger.debug("Excel parse failed: %s", exc)
        return set()


def _parse_html_table_accounts(html: str) -> Set[str]:
    """Extract account numbers from HTML tables."""
    if BS4_AVAILABLE:
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        accounts: Set[str] = set()
        for table in tables:
            text = table.get_text()
            accounts.update(_extract_accounts_from_text(text))
        return accounts
    return _extract_accounts_from_text(html)


def fetch_lgbs_accounts(session: requests.Session) -> Set[str]:
    """
    Crawl lgbs.com to find and parse the Harris County tax sale list.
    Returns a set of 13-digit HCAD account numbers found.
    """
    all_accounts: Set[str] = set()

    for root in LGBS_ROOTS:
        logger.info("Fetching LGBS root: %s", root)
        try:
            resp = session.get(root, timeout=20)
        except Exception as exc:
            logger.warning("  Cannot reach %s: %s", root, exc)
            continue

        if resp.status_code != 200:
            logger.warning("  HTTP %d from %s", resp.status_code, root)
            continue

        logger.info("  Connected to LGBS — scanning for Harris County links…")

        # Try extracting account numbers directly from the root page
        direct = _extract_accounts_from_text(resp.text)
        if direct:
            logger.info("  Found %d account numbers on root page", len(direct))
            all_accounts.update(direct)

        # Follow links that mention Harris County
        harris_links = _find_harris_links(resp.text, root)
        logger.info("  Found %d Harris County links to follow", len(harris_links))

        for link in harris_links[:15]:  # cap at 15 sub-pages
            logger.info("  Fetching: %s", link)
            try:
                time.sleep(1.5)
                r2 = session.get(link, timeout=25)
            except Exception as exc:
                logger.debug("    Error: %s", exc)
                continue

            if r2.status_code != 200:
                logger.debug("    HTTP %d", r2.status_code)
                continue

            content_type = r2.headers.get("Content-Type", "").lower()

            if "excel" in content_type or "spreadsheet" in content_type or link.endswith((".xlsx", ".xls")):
                accounts = _parse_excel_accounts(r2.content)
                logger.info("    Excel file — %d account numbers", len(accounts))
            elif "html" in content_type:
                # Check for links to Excel/CSV files on this sub-page
                sub_links = re.findall(r'href=["\']([^"\']+\.(?:xlsx|xls|csv))["\']', r2.text, re.IGNORECASE)
                for sl in sub_links[:5]:
                    full_sl = urljoin(link, sl)
                    logger.info("    Downloading file: %s", full_sl)
                    try:
                        time.sleep(1)
                        fr = session.get(full_sl, timeout=30)
                        if fr.status_code == 200:
                            if full_sl.endswith(".csv"):
                                try:
                                    file_df = pd.read_csv(io.BytesIO(fr.content), dtype=str)
                                    accounts = _extract_accounts_from_text(file_df.to_string())
                                except Exception:
                                    accounts = _extract_accounts_from_text(fr.text)
                            else:
                                accounts = _parse_excel_accounts(fr.content)
                            logger.info("      Found %d account numbers", len(accounts))
                            all_accounts.update(accounts)
                    except Exception as exc:
                        logger.debug("      Download error: %s", exc)

                # Also extract directly from HTML table
                html_accounts = _parse_html_table_accounts(r2.text)
                if html_accounts:
                    logger.info("    HTML table — %d account numbers", len(html_accounts))
                accounts = html_accounts
            else:
                accounts = _extract_accounts_from_text(r2.text)
                logger.info("    Raw text — %d account numbers", len(accounts))

            all_accounts.update(accounts)

        if all_accounts:
            logger.info("LGBS total: %d unique delinquent account numbers collected", len(all_accounts))
            break  # No need to try second root URL

    return all_accounts


# ── hctax.net Fallback ────────────────────────────────────────────────────────

def _check_one_hctax(acct: str, session: requests.Session) -> bool:
    """Check a single account number on hctax.net. Returns True if delinquent."""
    try:
        resp = session.get(HCTAX_URL, params={"acct": acct}, timeout=20)
        if resp.status_code != 200:
            return False
        text = resp.text.lower()
        return any(s in text for s in ["delinquent", "past due", "unpaid", "balance due", "amount due"])
    except Exception:
        return False


def fetch_hctax_accounts(acct_list: list, sample_size: int) -> Set[str]:
    session = _session()
    session.headers["Referer"] = "https://www.hctax.net/"
    delinquent: Set[str] = set()
    to_check = acct_list[:sample_size]
    total = len(to_check)
    eta_min = total * 3 // 60

    logger.info("Checking %d accounts on hctax.net — estimated %d min…", total, eta_min)

    for i, acct in enumerate(to_check, 1):
        if i % 25 == 0:
            logger.info("  %d / %d checked  |  %d delinquent found", i, total, len(delinquent))
        if _check_one_hctax(acct, session):
            delinquent.add(acct)
        time.sleep(random.uniform(2.0, 3.5))

    return delinquent


# ── Main runner ───────────────────────────────────────────────────────────────

def run(leads_path: str, output_path: str, hctax_sample: int) -> int:
    logger.info("=" * 56)
    logger.info("Phase 3 — Tax Delinquent Cross-Reference")
    logger.info("=" * 56)

    # Load leads
    logger.info("\nLoading %s …", leads_path)
    leads = pd.read_csv(leads_path, dtype=str, low_memory=False)
    logger.info("  %d leads loaded", len(leads))

    acct_col = "acct_num"
    if acct_col not in leads.columns:
        raise ValueError(f"'{acct_col}' column not found in {leads_path}")

    acct_list = leads[acct_col].dropna().str.strip().tolist()

    session = _session()

    # ── LGBS attempt ─────────────────────────────────────────────────────────
    logger.info("\n[1/2] Fetching LGBS Harris County tax sale list…")
    lgbs_accounts = fetch_lgbs_accounts(session)

    if lgbs_accounts:
        delinquent_accounts = lgbs_accounts
        source_label = "LGBS tax sale list"
        logger.info("LGBS SUCCESS — %d delinquent accounts", len(delinquent_accounts))
    else:
        # ── hctax.net fallback ────────────────────────────────────────────────
        logger.info("\nLGBS returned 0 results.")
        logger.info("[2/2] Falling back to hctax.net per-account lookup…")
        delinquent_accounts = fetch_hctax_accounts(acct_list, sample_size=hctax_sample)
        source_label = f"hctax.net (top {hctax_sample} leads checked)"

    # ── Cross-reference ───────────────────────────────────────────────────────
    if delinquent_accounts:
        mask = leads[acct_col].isin(delinquent_accounts)
        priority = leads[mask].copy()
        priority["tax_delinquent"] = f"YES — {source_label}"
        logger.info("\nCross-reference: %d of your leads are tax delinquent", len(priority))
    else:
        logger.warning(
            "\nUnable to retrieve delinquent tax data from LGBS or hctax.net.\n"
            "Try running from a different network or at a different time.\n"
            "Outputting all leads sorted by lead_score as fallback.\n"
        )
        leads["tax_delinquent"] = "NOT CHECKED"
        priority = leads.copy()

    # Sort by lead_score then appraised_value
    for col in ("lead_score", "appraised_value"):
        if col in priority.columns:
            priority[col] = pd.to_numeric(priority[col], errors="coerce")

    sort_cols = [c for c in ("lead_score", "appraised_value") if c in priority.columns]
    sort_asc  = [False] + [True] * (len(sort_cols) - 1)
    priority  = priority.sort_values(sort_cols, ascending=sort_asc).reset_index(drop=True)

    if "rank" not in priority.columns:
        priority.insert(0, "rank", range(1, len(priority) + 1))

    priority.to_csv(output_path, index=False)

    print()
    print("=" * 56)
    print("  PHASE 3 COMPLETE")
    print("=" * 56)
    print(f"  Source          : {source_label}")
    if "tax_delinquent" in priority.columns:
        hit_count = priority["tax_delinquent"].str.startswith("YES", na=False).sum()
    else:
        hit_count = 0
    print(f"  Delinquent hits : {hit_count}")
    print(f"  Output file     : {output_path}")
    print(f"  Total rows      : {len(priority)}")
    print("=" * 56)
    print()

    return len(priority)


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not BS4_AVAILABLE:
        print("\n[INFO] beautifulsoup4 not installed — HTML parsing limited.")
        print("  Run: pip install beautifulsoup4 openpyxl\n")

    parser = argparse.ArgumentParser(description="Phase 3 — Tax Delinquent Cross-Reference")
    parser.add_argument("--leads",        dest="leads",        default="leads.csv",          help="Input leads CSV (default: leads.csv)")
    parser.add_argument("--out",          dest="output",       default="priority_leads.csv", help="Output CSV (default: priority_leads.csv)")
    parser.add_argument("--hctax-sample", dest="hctax_sample", default=200, type=int,        help="Accounts to check on hctax.net if LGBS fails (default: 200)")
    args = parser.parse_args()

    run(args.leads, args.output, args.hctax_sample)
