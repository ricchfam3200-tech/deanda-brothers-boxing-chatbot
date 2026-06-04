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

Records with score >= 2 are included in leads.csv, sorted score DESC.

Usage
-----
  python lead_filter.py --in results.csv --out leads.csv
  python lead_filter.py --in results.csv --out leads.csv --min-score 3
"""

from __future__ import annotations

import argparse
import logging
from datetime import date

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

GOVERNMENT_KEYWORDS = [
    "HARRIS COUNTY", "HOUSTON CITY", "CITY OF HOUSTON",
    "HISD", "ALDINE ISD", "HUMBLE ISD", "SPRING ISD",
    "KATY ISD", "PASADENA ISD", "CYPRESS ISD",
    "STATE OF TEXAS", "TEXAS DOT", "TXDOT",
    "METRO", "PORT OF HOUSTON",
]


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

    # Out-of-state owner — mailing address doesn't end with a TX zip pattern
    if mail_addr and ", TX" not in mail_addr and "TEXAS" not in mail_addr:
        score += 2

    if "ESTATE" in owner:
        score += 2

    try:
        year = int(transfer[:4])
        if year >= 2020:
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


def is_government(owner: str) -> bool:
    owner_up = owner.upper()
    return any(kw in owner_up for kw in GOVERNMENT_KEYWORDS)


def run(input_path: str, output_path: str, min_score: int) -> int:
    logger.info("Reading %s …", input_path)
    df = pd.read_csv(input_path, dtype=str, low_memory=False)
    logger.info("  %d total records loaded", len(df))

    # Convert numeric columns
    for col in ("appraised_value", "market_value", "land_value", "building_value", "land_sqft"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Drop government-owned parcels
    mask_gov = df["owner_name"].fillna("").apply(is_government)
    df = df[~mask_gov].copy()
    logger.info("  %d records after removing government parcels", len(df))

    # Score every row
    df["lead_score"] = df.apply(score_row, axis=1)

    # Keep only records that meet the minimum score
    leads = df[df["lead_score"] >= min_score].copy()
    logger.info("  %d leads with score >= %d", len(leads), min_score)

    # Sort: score DESC, then appraised_value ASC (cheapest first within same score)
    leads = leads.sort_values(
        ["lead_score", "appraised_value"],
        ascending=[False, True],
    ).reset_index(drop=True)

    # Add rank column
    leads.insert(0, "rank", range(1, len(leads) + 1))

    # Add a human-readable reason column
    leads["signals"] = leads.apply(_signals_label, axis=1)

    leads.to_csv(output_path, index=False)
    logger.info("Saved %d leads → %s", len(leads), output_path)
    print(f"\n  Done — {len(leads)} motivated-seller leads saved to: {output_path}\n")
    return len(leads)


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HCAD Lead Filter")
    parser.add_argument("--in",        dest="input",     default="results.csv",  help="Input CSV (default: results.csv)")
    parser.add_argument("--out",       dest="output",    default="leads.csv",    help="Output CSV (default: leads.csv)")
    parser.add_argument("--min-score", dest="min_score", default=2, type=int,    help="Minimum lead score to include (default: 2)")
    args = parser.parse_args()

    run(args.input, args.output, args.min_score)
