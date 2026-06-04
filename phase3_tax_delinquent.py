#!/usr/bin/env python3
"""
phase3_tax_delinquent.py

Phase 3 — Harris County Tax Delinquent Cross-Reference

Pulls the upcoming Harris County tax sale list from the LGBS website
(Linebarger Goggan Blair & Sampson — Harris County's tax collection firm),
extracts delinquent account numbers, and cross-references them against
your leads.csv to produce priority_leads.csv.

A property in BOTH lists = owner is behind on taxes AND shows other
motivated-seller signals (absentee, estate, recent transfer, etc.).
Those are your #1 wholesale targets — mail them first.

Usage
-----
  python phase3_tax_delinquent.py --leads leads.csv --out priority_leads.csv
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import time
from typing import Set

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── LGBS Harris County tax sale pages ────────────────────────────────────────
LGBS_URLS = [
    "https://lgbs.com/harris-county-tax-sales/",
    "https://lgbs.com/upcoming-sales/harris/",
    "https://lgbs.com/texas/harris/",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

HCTAX_SEARCH = "https://www.hctax.net/Property/PropertyTaxes"

# HCAD account numbers are 13 digits
ACCT_PATTERN = re.compile(r"\b(\d{13})\b")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection":      "keep-alive",
    })
    return s


# ── Method 1: Scrape LGBS website ────────────────────────────────────────────

def fetch_lgbs_accounts() -> Set[str]:
    """Try each LGBS URL and extract 13-digit HCAD account numbers."""
    session = _session()
    found: Set[str] = set()

    for url in LGBS_URLS:
        logger.info("Trying LGBS URL: %s", url)
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code == 200:
                accounts = set(ACCT_PATTERN.findall(resp.text))
                if accounts:
                    logger.info("  Found %d account numbers at %s", len(accounts), url)
                    found.update(accounts)
                else:
                    logger.info("  Page loaded but no 13-digit account numbers found")
            else:
                logger.warning("  HTTP %d from %s", resp.status_code, url)
        except Exception as exc:
            logger.warning("  Error: %s", exc)
        time.sleep(2)

    return found


# ── Method 2: Batch lookup via hctax.net ─────────────────────────────────────

def check_hctax_delinquent(acct_num: str, session: requests.Session) -> bool:
    """
    Query hctax.net for a single account number.
    Returns True if any unpaid/delinquent taxes are detected.
    """
    try:
        resp = session.get(
            HCTAX_SEARCH,
            params={"acct": acct_num},
            timeout=20,
        )
        if resp.status_code != 200:
            return False
        text = resp.text.lower()
        # Look for delinquent indicators in the page
        signals = ["delinquent", "past due", "unpaid", "balance due", "amount due"]
        return any(s in text for s in signals)
    except Exception:
        return False


def fetch_hctax_accounts(acct_nums: list, sample_size: int = 200) -> Set[str]:
    """
    Check a sample of account numbers against hctax.net.
    Checks top sample_size leads only to stay within a reasonable time window.
    """
    session = _session()
    delinquent: Set[str] = set()
    to_check = acct_nums[:sample_size]
    total = len(to_check)

    logger.info("Checking %d accounts via hctax.net (this takes ~%d minutes)…",
                total, total * 3 // 60)

    for i, acct in enumerate(to_check, 1):
        if i % 20 == 0:
            logger.info("  Progress: %d / %d checked, %d delinquent so far",
                        i, total, len(delinquent))
        is_delinquent = check_hctax_delinquent(acct, session)
        if is_delinquent:
            delinquent.add(acct)
        time.sleep(random.uniform(2.0, 3.5))

    return delinquent


# ── Cross-reference and output ────────────────────────────────────────────────

def run(leads_path: str, output_path: str, hctax_sample: int) -> int:
    logger.info("Reading leads from %s …", leads_path)
    leads = pd.read_csv(leads_path, dtype=str, low_memory=False)
    logger.info("  %d leads loaded", len(leads))

    if "acct_num" not in leads.columns:
        raise ValueError("leads.csv must have an 'acct_num' column")

    acct_nums = leads["acct_num"].dropna().str.strip().tolist()

    # ── Step 1: Try LGBS scrape ───────────────────────────────────────────────
    logger.info("\n[Step 1] Fetching LGBS Harris County tax sale list…")
    lgbs_accounts = fetch_lgbs_accounts()

    if lgbs_accounts:
        logger.info("LGBS: %d delinquent account numbers found", len(lgbs_accounts))
        priority = leads[leads["acct_num"].isin(lgbs_accounts)].copy()
        priority["tax_delinquent"] = "YES - on LGBS tax sale list"
        method = "LGBS"
    else:
        # ── Step 2: Fall back to hctax.net sample check ──────────────────────
        logger.info("LGBS scrape returned 0 results — falling back to hctax.net lookup")
        logger.info("[Step 2] Checking top %d leads on hctax.net…", hctax_sample)
        delinquent_accounts = fetch_hctax_accounts(acct_nums, sample_size=hctax_sample)

        if delinquent_accounts:
            logger.info("hctax.net: %d delinquent accounts found", len(delinquent_accounts))
            priority = leads[leads["acct_num"].isin(delinquent_accounts)].copy()
            priority["tax_delinquent"] = "YES - delinquent per hctax.net"
            method = "hctax.net"
        else:
            logger.warning(
                "\nCould not retrieve delinquent tax data from either source.\n"
                "This usually means the server is blocking automated requests.\n"
                "Try running from a different network or at a different time.\n"
                "\nFalling back: outputting all leads sorted by lead_score.\n"
            )
            leads["tax_delinquent"] = "NOT CHECKED"
            leads.to_csv(output_path, index=False)
            print(f"\n  Saved {len(leads)} leads (no tax filter applied) → {output_path}\n")
            return len(leads)

    # Sort by lead_score DESC within the priority set
    if "lead_score" in priority.columns:
        priority["lead_score"] = pd.to_numeric(priority["lead_score"], errors="coerce")
        priority = priority.sort_values("lead_score", ascending=False)

    priority.to_csv(output_path, index=False)
    logger.info("\nPriority leads saved → %s", output_path)
    print(f"\n  Method: {method}")
    print(f"  {len(priority)} Priority 1 leads (tax delinquent + motivated seller signals)")
    print(f"  Saved → {output_path}\n")
    return len(priority)


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 3 — Tax Delinquent Cross-Reference")
    parser.add_argument("--leads",        default="leads.csv",          help="Input leads CSV (default: leads.csv)")
    parser.add_argument("--out",          default="priority_leads.csv", help="Output CSV (default: priority_leads.csv)")
    parser.add_argument("--hctax-sample", default=200, type=int,        help="How many accounts to check on hctax.net if LGBS fails (default: 200)")
    args = parser.parse_args()

    run(args.leads, args.out, args.hctax_sample)
