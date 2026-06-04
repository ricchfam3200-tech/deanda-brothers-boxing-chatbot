#!/usr/bin/env python3
"""
phase4_foreclosures.py

Phase 4 — Harris County Foreclosure Detection

Three methods, tried in order:

  1. Harris County County Clerk (cclerk.hctx.net)
     Fetches the page first to grab ASP.NET ViewState, then POSTs
     a proper search for Substitute Trustee Sale / Lis Pendens docs.

  2. HCAD owner-name analysis (always works — uses data you already have)
     Flags properties in your leads whose owner name indicates the bank /
     servicer already took the property (TRUSTEE, BANK, MORTGAGE, etc.)
     and properties with zero building value (vacant / demolished).

  3. Low-equity distress signal
     Flags A1 properties where appraised_value <= 80,000 AND land_value
     is a high % of total value — usually means structure is in bad shape.

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

CCLERK_BASE = "https://www.cclerk.hctx.net/applications/websearch/RD.aspx"

# Keywords that indicate bank / servicer ownership in owner_name
BANK_KEYWORDS = [
    "TRUSTEE", "TRUST SERV", "BANK", "BANCORP", "FINANCIAL",
    "MORTGAGE", "LENDING", "SERVICER", "FEDERAL HOME",
    "FANNIE MAE", "FREDDIE MAC", "HUD ", "FHA ", "VA LOAN",
    "WELLS FARGO", "CHASE", "CITIBANK", "NATIONSTAR",
    "OCWEN", "PHH MORTGAGE", "CARRINGTON", "SELENE",
    "NEWREZ", "LOANCARE", "BSI FINANCIAL", "LAKEVIEW",
    "MR COOPER", "PENNYMAC", "CALIBER HOME", "FREEDOM MORTGAGE",
]


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "text/html,application/xhtml+xml,*/*;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
    })
    return s


# ── Method 1: Harris County County Clerk ─────────────────────────────────────

def _extract_viewstate(html: str) -> dict:
    """Pull ASP.NET hidden form fields needed for POST."""
    fields = {}
    for field in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        m = re.search(rf'id="{field}"\s+value="([^"]*)"', html)
        if m:
            fields[field] = m.group(1)
    return fields


def fetch_cclerk_foreclosures(session: requests.Session) -> set:
    """
    POST a proper search to the Harris County County Clerk recorded
    document search for Substitute Trustee Sale and Lis Pendens filings.
    Returns a set of owner/grantor names found.
    """
    names: set = set()
    session.headers["Referer"] = CCLERK_BASE

    # Step 1: GET the page to collect ViewState
    logger.info("  Fetching County Clerk page for ViewState…")
    try:
        r = session.get(CCLERK_BASE, timeout=20)
    except Exception as exc:
        logger.warning("  Cannot reach County Clerk: %s", exc)
        return names

    if r.status_code != 200:
        logger.warning("  HTTP %d from County Clerk", r.status_code)
        return names

    vs = _extract_viewstate(r.text)
    if not vs:
        logger.warning("  Could not extract ViewState — site may have changed")
        return names

    logger.info("  ViewState found — submitting searches…")

    # Step 2: POST for each foreclosure document type
    doc_types = ["SUB TRUSTEE", "LIS PENDENS", "TRUSTEE SALE", "FORECLOSURE"]

    for doc_type in doc_types:
        form_data = {
            "__VIEWSTATE":          vs.get("__VIEWSTATE", ""),
            "__VIEWSTATEGENERATOR": vs.get("__VIEWSTATEGENERATOR", ""),
            "__EVENTVALIDATION":    vs.get("__EVENTVALIDATION", ""),
            "__EVENTTARGET":        "",
            "__EVENTARGUMENT":      "",
            "ctl00$ContentPlaceHolder1$txtDocType": doc_type,
            "ctl00$ContentPlaceHolder1$btnSearch":  "Search",
        }

        try:
            time.sleep(1.5)
            resp = session.post(CCLERK_BASE, data=form_data, timeout=25)
        except Exception as exc:
            logger.debug("  POST error for %s: %s", doc_type, exc)
            continue

        if resp.status_code != 200:
            continue

        # Extract grantor/grantee names from results
        if BS4:
            soup = BeautifulSoup(resp.text, "html.parser")
            for td in soup.find_all("td"):
                val = td.get_text(strip=True).upper()
                if 4 < len(val) < 60 and not val.isdigit():
                    names.add(val)
        else:
            found = re.findall(r'\b([A-Z]{2,}(?:\s[A-Z]{2,}){1,4})\b', resp.text)
            names.update(found)

        logger.info("  Doc type '%s': %d name entries collected", doc_type, len(names))

    return names


# ── Method 2: HCAD owner-name analysis ───────────────────────────────────────

def flag_bank_owned(leads: pd.DataFrame) -> pd.DataFrame:
    """Flag leads where the owner name matches a bank / servicer / trustee."""
    def is_bank(owner: str) -> bool:
        up = str(owner).upper()
        return any(kw in up for kw in BANK_KEYWORDS)

    mask = leads["owner_name"].fillna("").apply(is_bank)
    result = leads[mask].copy()
    result["foreclosure_filing"] = "YES — bank/servicer owned (REO)"
    logger.info("  Bank/trustee-owned properties: %d", len(result))
    return result


def flag_vacant_distressed(leads: pd.DataFrame) -> pd.DataFrame:
    """Flag A1 properties with zero or missing building value — structure gone."""
    df = leads.copy()
    for col in ("building_value", "appraised_value", "land_value"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    mask = (
        (df.get("property_type", pd.Series(dtype=str)) == "A1") &
        (df.get("building_value", pd.Series(dtype=float)).fillna(0) == 0)
    )
    result = df[mask].copy()
    result["foreclosure_filing"] = "YES — A1 with no structure (likely vacant/demolished)"
    logger.info("  A1 vacant/no structure properties: %d", len(result))
    return result


def flag_low_equity_distress(leads: pd.DataFrame) -> pd.DataFrame:
    """
    Flag A1 single-family homes with appraised value <= $80K AND land
    makes up 70%+ of total value — strong indicator of severe disrepair.
    """
    df = leads.copy()
    for col in ("building_value", "appraised_value", "land_value"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    appr = df.get("appraised_value", pd.Series(dtype=float)).fillna(0)
    land = df.get("land_value",      pd.Series(dtype=float)).fillna(0)
    bld  = df.get("building_value",  pd.Series(dtype=float)).fillna(0)

    land_pct = land / appr.replace(0, float("nan"))

    mask = (
        (df.get("property_type", pd.Series(dtype=str)) == "A1") &
        (appr > 0) &
        (appr <= 80_000) &
        (land_pct >= 0.70)
    )
    result = df[mask].copy()
    result["foreclosure_filing"] = "YES — severe distress (low value, land-heavy)"
    logger.info("  Low-equity severely distressed A1 properties: %d", len(result))
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def run(leads_path: str, output_path: str) -> int:
    logger.info("=" * 56)
    logger.info("Phase 4 — Foreclosure Detection")
    logger.info("=" * 56)

    leads = pd.read_csv(leads_path, dtype=str, low_memory=False)
    logger.info("Loaded %d leads from %s", len(leads), leads_path)

    all_flagged = pd.DataFrame()

    # ── Method 1: County Clerk ────────────────────────────────────────────────
    logger.info("\n[1/3] Querying Harris County County Clerk…")
    session = _session()
    clerk_names = fetch_cclerk_foreclosures(session)

    if clerk_names:
        # Match against lead owner names
        def clerk_match(owner: str) -> bool:
            up = str(owner).upper().strip()
            return any(up in n or n in up for n in clerk_names if len(n) > 5)

        mask = leads["owner_name"].fillna("").apply(clerk_match)
        clerk_hits = leads[mask].copy()
        clerk_hits["foreclosure_filing"] = "YES — County Clerk filing"
        logger.info("County Clerk matches: %d", len(clerk_hits))
        all_flagged = pd.concat([all_flagged, clerk_hits], ignore_index=True)
    else:
        logger.info("County Clerk returned 0 results — proceeding to HCAD analysis")

    # ── Method 2: Bank/trustee-owned ─────────────────────────────────────────
    logger.info("\n[2/3] Scanning HCAD owner names for bank/servicer ownership…")
    bank_hits = flag_bank_owned(leads)
    all_flagged = pd.concat([all_flagged, bank_hits], ignore_index=True)

    # ── Method 3: Vacant/distressed A1 ───────────────────────────────────────
    logger.info("\n[3/3] Flagging vacant and severely distressed A1 properties…")
    vacant_hits   = flag_vacant_distressed(leads)
    distress_hits = flag_low_equity_distress(leads)
    all_flagged = pd.concat([all_flagged, vacant_hits, distress_hits], ignore_index=True)

    # ── Deduplicate ───────────────────────────────────────────────────────────
    if not all_flagged.empty and "acct_num" in all_flagged.columns:
        all_flagged = all_flagged.drop_duplicates(subset="acct_num", keep="first")

    logger.info("\nTotal unique flagged properties: %d", len(all_flagged))

    if all_flagged.empty:
        logger.warning("No properties flagged — saving full lead list as fallback")
        leads["foreclosure_filing"] = "NOT CHECKED"
        leads.to_csv(output_path, index=False)
        print(f"\n  Saved {len(leads)} leads (no foreclosure filter) → {output_path}\n")
        return len(leads)

    # Sort
    for col in ("lead_score", "appraised_value"):
        if col in all_flagged.columns:
            all_flagged[col] = pd.to_numeric(all_flagged[col], errors="coerce")

    sort_cols = [c for c in ("lead_score", "appraised_value") if c in all_flagged.columns]
    sort_asc  = [False] + [True] * (len(sort_cols) - 1)
    all_flagged = all_flagged.sort_values(sort_cols, ascending=sort_asc).reset_index(drop=True)
    all_flagged.to_csv(output_path, index=False)

    print()
    print("=" * 56)
    print("  PHASE 4 COMPLETE")
    print("=" * 56)
    print(f"  Bank/servicer owned  : {len(bank_hits)}")
    print(f"  Vacant/no structure  : {len(vacant_hits)}")
    print(f"  Severely distressed  : {len(distress_hits)}")
    print(f"  County Clerk matches : {len(clerk_names) > 0 and len(all_flagged) or 0}")
    print(f"  Total unique flagged : {len(all_flagged)}")
    print(f"  Output file          : {output_path}")
    print("=" * 56)
    print()

    return len(all_flagged)


if __name__ == "__main__":
    if not BS4:
        print("\n[INFO] Run: pip install beautifulsoup4 openpyxl\n")

    parser = argparse.ArgumentParser(description="Phase 4 — Foreclosure Detection")
    parser.add_argument("--leads", default="leads.csv",             help="Input leads CSV")
    parser.add_argument("--out",   default="foreclosure_leads.csv", help="Output CSV")
    args = parser.parse_args()

    run(args.leads, args.out)
