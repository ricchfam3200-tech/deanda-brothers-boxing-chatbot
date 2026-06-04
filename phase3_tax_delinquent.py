#!/usr/bin/env python3
"""
phase3_tax_delinquent.py

Phase 3 — Harris County Tax Delinquent Cross-Reference

Two modes:

  AUTO  — Tries to pull the LGBS Harris County delinquent list from lgbs.com
          automatically.  If the site blocks the request, falls back to
          checking hctax.net per-account for delinquency.

  MANUAL — You download the LGBS list yourself (takes 2 minutes) and pass
           it in with --lgbs-file.  Always works.  Handles Excel and CSV.

HOW TO GET THE LGBS LIST MANUALLY
----------------------------------
1. Open your browser and go to:  https://lgbs.com
2. Click "Tax Sales" → "Harris County" (or search for Harris County)
3. Download the upcoming sale list — it will be an Excel (.xlsx) or CSV file
4. Save it anywhere, e.g. C:\\Users\\loco3\\Downloads\\lgbs_harris.xlsx
5. Run:
   python phase3_tax_delinquent.py --lgbs-file lgbs_harris.xlsx --leads leads.csv --out priority_leads.csv

Usage
-----
  # Auto mode (tries LGBS + hctax.net automatically):
  python phase3_tax_delinquent.py --leads leads.csv --out priority_leads.csv

  # Manual mode (always works):
  python phase3_tax_delinquent.py --lgbs-file lgbs_harris.xlsx --leads leads.csv --out priority_leads.csv
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import random
import re
import time
from typing import Set
from urllib.parse import urljoin

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

ACCT_PATTERN = re.compile(r"\b(\d{13})\b")

LGBS_URLS = [
    "https://lgbs.com/harris-county-tax-sales/",
    "https://lgbs.com/harris-county/",
    "https://lgbs.com/upcoming-sales/harris/",
    "https://lgbs.com/texas/harris/",
    "https://lgbs.com/",
]

HCTAX_SEARCH = "https://www.hctax.net/Property/PropertySearch"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection":      "keep-alive",
    })
    return s


# ── Manual LGBS file parsing ──────────────────────────────────────────────────

def parse_lgbs_file(path: str) -> Set[str]:
    """
    Read a manually downloaded LGBS Excel or CSV file and extract
    all 13-digit HCAD account numbers found in any column.
    """
    logger.info("Reading LGBS file: %s", path)
    ext = os.path.splitext(path)[1].lower()

    try:
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(path, dtype=str, engine="openpyxl")
        else:
            # Try CSV; handle common encodings
            try:
                df = pd.read_csv(path, dtype=str, encoding="utf-8")
            except UnicodeDecodeError:
                df = pd.read_csv(path, dtype=str, encoding="latin-1")

        text = df.to_string()
        accounts = set(ACCT_PATTERN.findall(text))
        logger.info("  Found %d HCAD account numbers in file", len(accounts))

        if not accounts:
            # Fallback: look for 8-10 digit partial account numbers
            partial = re.findall(r"\b(\d{8,12})\b", text)
            if partial:
                logger.info("  Found %d partial account numbers (will try prefix match)", len(partial))
                return {p.zfill(13) for p in partial}

        return accounts

    except Exception as exc:
        logger.error("Could not read LGBS file: %s", exc)
        return set()


# ── Auto: scrape LGBS website ─────────────────────────────────────────────────

def fetch_lgbs_auto(session: requests.Session) -> Set[str]:
    """Try to pull LGBS Harris County sale list automatically."""
    all_accounts: Set[str] = set()

    for url in LGBS_URLS:
        logger.info("Trying LGBS URL: %s", url)
        try:
            resp = session.get(url, timeout=20)
        except Exception as exc:
            logger.warning("  Cannot reach %s — %s", url, exc)
            continue

        if resp.status_code != 200:
            logger.warning("  HTTP %d", resp.status_code)
            continue

        # Direct account numbers on page
        direct = set(ACCT_PATTERN.findall(resp.text))
        all_accounts.update(direct)

        # Look for downloadable file links
        file_links = re.findall(
            r'href=["\']([^"\']+\.(?:xlsx|xls|csv|pdf))["\']',
            resp.text, re.IGNORECASE
        )
        for fl in file_links[:5]:
            full = urljoin(url, fl)
            if "harris" in full.lower() or "harris" in resp.text.lower():
                logger.info("  Found file link: %s", full)
                try:
                    time.sleep(1)
                    fr = session.get(full, timeout=30)
                    if fr.status_code == 200 and not full.endswith(".pdf"):
                        if full.endswith(".csv"):
                            try:
                                file_df = pd.read_csv(io.BytesIO(fr.content), dtype=str)
                            except Exception:
                                file_df = pd.DataFrame()
                        else:
                            try:
                                file_df = pd.read_excel(io.BytesIO(fr.content), dtype=str, engine="openpyxl")
                            except Exception:
                                file_df = pd.DataFrame()
                        if not file_df.empty:
                            accts = set(ACCT_PATTERN.findall(file_df.to_string()))
                            logger.info("    %d accounts from file", len(accts))
                            all_accounts.update(accts)
                except Exception as exc:
                    logger.debug("  File download error: %s", exc)

        if all_accounts:
            logger.info("LGBS auto: %d account numbers found", len(all_accounts))
            break
        time.sleep(2)

    return all_accounts


# ── Auto: hctax.net per-account fallback ─────────────────────────────────────

def _check_hctax(acct: str, session: requests.Session) -> bool:
    """Check one account on hctax.net. Returns True if delinquent signals found."""
    for url in [
        f"https://www.hctax.net/Property/PropertyTaxes?AccountNumber={acct}",
        f"https://www.hctax.net/Property/PropertySearch?searchTerm={acct}",
        f"https://www.hctax.net/Property/PropertyTaxes?acct={acct}",
    ]:
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 200:
                text = r.text.lower()
                if any(s in text for s in ["delinquent", "past due", "unpaid", "balance due"]):
                    return True
        except Exception:
            pass
    return False


def fetch_hctax_accounts(acct_list: list, sample_size: int) -> Set[str]:
    session = _session()
    session.headers["Referer"] = "https://www.hctax.net/"
    delinquent: Set[str] = set()
    to_check = acct_list[:sample_size]

    logger.info("Checking %d accounts on hctax.net…", len(to_check))
    for i, acct in enumerate(to_check, 1):
        if i % 25 == 0:
            logger.info("  %d / %d  |  %d delinquent", i, len(to_check), len(delinquent))
        if _check_hctax(acct, session):
            delinquent.add(acct)
        time.sleep(random.uniform(2.0, 3.0))

    return delinquent


# ── Main ──────────────────────────────────────────────────────────────────────

def run(leads_path: str,
        output_path: str,
        lgbs_file: str,
        hctax_sample: int) -> int:

    logger.info("=" * 56)
    logger.info("Phase 3 — Tax Delinquent Cross-Reference")
    logger.info("=" * 56)

    leads = pd.read_csv(leads_path, dtype=str, low_memory=False)
    logger.info("Loaded %d leads from %s", len(leads), leads_path)

    acct_list = leads["acct_num"].dropna().str.strip().tolist()
    delinquent_accounts: Set[str] = set()
    source = ""

    # ── Manual LGBS file (highest priority) ──────────────────────────────────
    if lgbs_file:
        if not os.path.exists(lgbs_file):
            logger.error("LGBS file not found: %s", lgbs_file)
        else:
            delinquent_accounts = parse_lgbs_file(lgbs_file)
            source = f"LGBS file: {lgbs_file}"

    # ── Auto LGBS ─────────────────────────────────────────────────────────────
    if not delinquent_accounts:
        logger.info("\n[1/2] Auto-fetching LGBS Harris County list…")
        session = _session()
        delinquent_accounts = fetch_lgbs_auto(session)
        if delinquent_accounts:
            source = "LGBS (auto)"

    # ── hctax.net fallback ────────────────────────────────────────────────────
    if not delinquent_accounts:
        logger.info("\nLGBS returned 0 — falling back to hctax.net…")
        logger.info("[2/2] Checking top %d leads on hctax.net…", hctax_sample)
        delinquent_accounts = fetch_hctax_accounts(acct_list, hctax_sample)
        if delinquent_accounts:
            source = f"hctax.net (top {hctax_sample} checked)"

    # ── No data found ─────────────────────────────────────────────────────────
    if not delinquent_accounts:
        print()
        print("=" * 56)
        print("  PHASE 3 — MANUAL DOWNLOAD REQUIRED")
        print("=" * 56)
        print()
        print("  Automated LGBS and hctax.net requests were blocked.")
        print("  Get the list manually in 2 minutes:")
        print()
        print("  1. Open browser → go to:  https://lgbs.com")
        print("  2. Click Tax Sales → Harris County")
        print("  3. Download the upcoming sale list (Excel or CSV)")
        print("  4. Save it to your hcad-project folder")
        print("  5. Re-run with:")
        print()
        print("     python phase3_tax_delinquent.py --lgbs-file lgbs_harris.xlsx")
        print()
        print("=" * 56)
        leads["tax_delinquent"] = "NOT CHECKED"
        leads.to_csv(output_path, index=False)
        return 0

    # ── Cross-reference ───────────────────────────────────────────────────────
    mask = leads["acct_num"].isin(delinquent_accounts)
    priority = leads[mask].copy()
    priority["tax_delinquent"] = f"YES — {source}"

    for col in ("lead_score", "appraised_value"):
        if col in priority.columns:
            priority[col] = pd.to_numeric(priority[col], errors="coerce")

    sort_cols = [c for c in ("lead_score", "appraised_value") if c in priority.columns]
    sort_asc  = [False] + [True] * (len(sort_cols) - 1)
    priority = priority.sort_values(sort_cols, ascending=sort_asc).reset_index(drop=True)
    priority.to_csv(output_path, index=False)

    print()
    print("=" * 56)
    print("  PHASE 3 COMPLETE")
    print("=" * 56)
    print(f"  Source          : {source}")
    print(f"  Delinquent hits : {len(priority)}")
    print(f"  Output file     : {output_path}")
    print("=" * 56)
    print()
    return len(priority)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 3 — Tax Delinquent Cross-Reference")
    parser.add_argument("--leads",        default="leads.csv",          help="Input leads CSV")
    parser.add_argument("--out",          default="priority_leads.csv", help="Output CSV")
    parser.add_argument("--lgbs-file",    default="",                   help="Path to manually downloaded LGBS Excel/CSV file")
    parser.add_argument("--hctax-sample", default=200, type=int,        help="Accounts to check on hctax.net if LGBS fails")
    args = parser.parse_args()

    run(args.leads, args.out, args.lgbs_file, args.hctax_sample)
