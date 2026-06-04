#!/usr/bin/env python3
"""
lead_filter.py

Reads results.csv (output from batch_processor.py) and produces a
clean leads.csv containing only motivated-seller candidates, ranked
by a simple lead score.

Motivated-seller signals scored:
  +3  Property type = A1 (single-family residential)
  +2  Owner mailing address is outside Texas (absentee / out-of-state)
  +2  Owner name contains ESTATE (probate situation)
  +2  Transfer date is 2020 or later (recent acquisition — may be distressed)
  +1  Appraised value <= $150,000 (lower-value = easier wholesale deal)
  +1  Building value = 0 or blank (vacant lot or structure issue)

By default only INDIVIDUAL owners are kept (no LLCs, banks, or corporations).
Use --include-businesses to keep all owner types.

Records with score >= 2 are included in leads.csv, sorted score DESC.

Usage
-----
  python lead_filter.py --in results.csv --out leads.csv
  python lead_filter.py --in results.csv --out leads.csv --min-score 3
  python lead_filter.py --in results.csv --out leads.csv --include-businesses
"""

from __future__ import annotations

import argparse
import logging

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── Owner type filters ────────────────────────────────────────────────────────

GOVERNMENT_KEYWORDS = [
    "HARRIS COUNTY", "HOUSTON CITY", "CITY OF HOUSTON",
    "HISD", "ALDINE ISD", "HUMBLE ISD", "SPRING ISD",
    "KATY ISD", "PASADENA ISD", "CYPRESS ISD",
    "STATE OF TEXAS", "TEXAS DOT", "TXDOT",
    "METRO", "PORT OF HOUSTON",
]

BUSINESS_KEYWORDS = [
    " LLC", " L.L.C", " INC", " INCORPORATED", " CORP",
    " CORPORATION", " LTD", " L.P.", " LP ", " LP,",
    " PARTNERS", " PARTNERSHIP", " HOLDINGS", " HOLDING",
    " PROPERTIES", " PROPERTY", " INVESTMENTS", " INVESTMENT",
    " REALTY", " REAL ESTATE", " GROUP", " COMPANY", " CO.",
    " MANAGEMENT", " ENTERPRISES", " ENTERPRISE", " SERVICES",
    " SERVICE", " SOLUTIONS", " VENTURES", " VENTURE",
    " CAPITAL", " ACQUISITIONS", " ACQUISITION", " FUNDING",
    " ASSETS", " ASSET", " EQUITY", " EQUITIES",
    " BANK", " BANKING", " FINANCIAL", " FINANCE",
    " MORTGAGE", " LENDING", " LOAN", " CREDIT",
    " TRUST SERV", " TRUSTEE", " GUARDIAN",
    " CHURCH", " MINISTRY", " MINISTRIES",
    " HOUSING", " DEVELOPMENT", " DEVELOPERS",
]


def is_government(owner: str) -> bool:
    up = owner.upper()
    return any(kw in up for kw in GOVERNMENT_KEYWORDS)


def is_business(owner: str) -> bool:
    """Return True if the owner name looks like a business, LLC, or institution."""
    up = " " + owner.upper() + " "
    return any(kw in up for kw in BUSINESS_KEYWORDS)


def is_individual(owner: str) -> bool:
    """Return True if the owner name looks like a real person."""
    if not owner or not owner.strip():
        return False
    if is_government(owner):
        return False
    if is_business(owner):
        # Exception: "ESTATE OF ..." is still an individual situation
        if "ESTATE OF" in owner.upper():
            return True
        return False
    return True


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_row(row: pd.Series) -> int:
    score = 0
    owner      = str(row.get("owner_name", "") or "").upper()
    mail_addr  = str(row.get("mailing_address", "") or "").upper()
    prop_type  = str(row.get("property_type", "") or "").upper().strip()
    transfer   = str(row.get("transfer_date", "") or "")
    appr_val   = row.get("appraised_value")
    bld_val    = row.get("building_value")

    if prop_type == "A1":
        score += 3

    if mail_addr and ", TX" not in mail_addr and "TEXAS" not in mail_addr:
        score += 2

    if "ESTATE" in owner:
        score += 2

    try:
        if int(transfer[:4]) >= 2020:
            score += 2
    except (ValueError, TypeError):
        pass

    try:
        if float(appr_val) <= 150_000:
            score += 1
    except (ValueError, TypeError):
        pass

    try:
        if float(bld_val) == 0:
            score += 1
    except (ValueError, TypeError):
        if not bld_val or str(bld_val).strip() in ("", "nan", "None"):
            score += 1

    return score


def _signals_label(row: pd.Series) -> str:
    tags = []
    owner     = str(row.get("owner_name", "") or "").upper()
    mail_addr = str(row.get("mailing_address", "") or "").upper()
    prop_type = str(row.get("property_type", "") or "").upper().strip()
    transfer  = str(row.get("transfer_date", "") or "")
    appr_val  = row.get("appraised_value")
    bld_val   = row.get("building_value")

    if prop_type == "A1":
        tags.append("single-family")
    if mail_addr and ", TX" not in mail_addr and "TEXAS" not in mail_addr:
        tags.append("out-of-state owner")
    if "ESTATE" in owner:
        tags.append("probate/estate")
    try:
        if int(transfer[:4]) >= 2020:
            tags.append("recent transfer")
    except (ValueError, TypeError):
        pass
    try:
        if float(appr_val) <= 150_000:
            tags.append("low value")
    except (ValueError, TypeError):
        pass
    try:
        if float(bld_val) == 0:
            tags.append("vacant/no structure")
    except (ValueError, TypeError):
        if not bld_val or str(bld_val).strip() in ("", "nan", "None"):
            tags.append("vacant/no structure")

    return " | ".join(tags)


# ── Main ──────────────────────────────────────────────────────────────────────

def run(input_path: str, output_path: str, min_score: int, individuals_only: bool) -> int:
    logger.info("Reading %s …", input_path)
    df = pd.read_csv(input_path, dtype=str, low_memory=False)
    logger.info("  %d total records loaded", len(df))

    for col in ("appraised_value", "market_value", "land_value", "building_value", "land_sqft"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Remove government parcels
    mask_gov = df["owner_name"].fillna("").apply(is_government)
    df = df[~mask_gov].copy()
    logger.info("  %d records after removing government parcels", len(df))

    # Remove businesses/LLCs if individuals-only mode
    if individuals_only:
        mask_biz = df["owner_name"].fillna("").apply(lambda x: not is_individual(x))
        df = df[~mask_biz].copy()
        logger.info("  %d records after keeping only individual owners", len(df))

    df["lead_score"] = df.apply(score_row, axis=1)

    leads = df[df["lead_score"] >= min_score].copy()
    logger.info("  %d leads with score >= %d", len(leads), min_score)

    leads = leads.sort_values(
        ["lead_score", "appraised_value"],
        ascending=[False, True],
    ).reset_index(drop=True)

    if "rank" in leads.columns:
        leads = leads.drop(columns=["rank"])
    leads.insert(0, "rank", range(1, len(leads) + 1))
    leads["signals"] = leads.apply(_signals_label, axis=1)

    leads.to_csv(output_path, index=False)
    logger.info("Saved %d leads → %s", len(leads), output_path)
    print(f"\n  Done — {len(leads)} individual motivated-seller leads saved to: {output_path}\n")
    return len(leads)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HCAD Lead Filter")
    parser.add_argument("--in",               dest="input",             default="results.csv", help="Input CSV")
    parser.add_argument("--out",              dest="output",            default="leads.csv",   help="Output CSV")
    parser.add_argument("--min-score",        dest="min_score",         default=2, type=int,   help="Minimum lead score (default: 2)")
    parser.add_argument("--include-businesses", dest="include_biz",     action="store_true",   help="Include LLCs and corporations (default: individuals only)")
    args = parser.parse_args()

    run(args.input, args.output, args.min_score, individuals_only=not args.include_biz)
