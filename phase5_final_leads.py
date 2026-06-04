#!/usr/bin/env python3
"""
phase5_final_leads.py

Phase 5 — Final Lead Aggregator & Scoring

Combines all phase outputs into one ranked final_leads.csv.

Inputs (use whatever you have — all are optional except leads.csv):
  leads.csv              Phase 2 output — all motivated seller leads
  priority_leads.csv     Phase 3 output — tax delinquent subset
  foreclosure_leads.csv  Phase 4 output — foreclosure filing subset

Scoring (max 10 points):
  +3  Single-family residential (A1)
  +2  Out-of-state / absentee owner
  +2  Tax delinquent (on LGBS list)
  +2  Active foreclosure filing
  +1  Probate / estate situation
  +1  Transfer date 2020 or later
  +1  Appraised value <= $150,000
  +1  No building value (vacant lot)

Output columns added:
  final_score       0-10 composite score
  final_signals     human-readable list of all signals hit
  mail_name         formatted owner name for mail merge
  mail_address      formatted property address for mail merge

Usage
-----
  python phase5_final_leads.py
  python phase5_final_leads.py --leads leads.csv --priority priority_leads.csv --foreclosure foreclosure_leads.csv --out final_leads.csv
"""

from __future__ import annotations

import argparse
import logging
import os

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _load_if_exists(path: str) -> pd.DataFrame:
    if path and os.path.exists(path):
        df = pd.read_csv(path, dtype=str, low_memory=False)
        logger.info("  Loaded %d rows from %s", len(df), path)
        return df
    logger.info("  Skipping %s (not found)", path)
    return pd.DataFrame()


def _yes_flag(val: str) -> bool:
    return str(val).upper().startswith("YES")


def score_and_label(row: pd.Series,
                    delinquent_accts: set,
                    foreclosure_accts: set) -> tuple:
    score = 0
    signals = []

    owner     = str(row.get("owner_name", "") or "").upper()
    mail_addr = str(row.get("mailing_address", "") or "").upper()
    prop_type = str(row.get("property_type", "") or "").upper().strip()
    transfer  = str(row.get("transfer_date", "") or "")
    appr_val  = row.get("appraised_value")
    bld_val   = row.get("building_value")
    acct      = str(row.get("acct_num", "") or "").strip()

    # Single-family
    if prop_type == "A1":
        score += 3
        signals.append("single-family")

    # Out-of-state
    if mail_addr and ", TX" not in mail_addr and "TEXAS" not in mail_addr:
        score += 2
        signals.append("out-of-state owner")

    # Tax delinquent
    td_flag = row.get("tax_delinquent", "")
    if _yes_flag(td_flag) or acct in delinquent_accts:
        score += 2
        signals.append("TAX DELINQUENT")

    # Foreclosure
    fc_flag = row.get("foreclosure_filing", "")
    if _yes_flag(fc_flag) or acct in foreclosure_accts:
        score += 2
        signals.append("FORECLOSURE FILED")

    # Estate / probate
    if "ESTATE" in owner:
        score += 1
        signals.append("probate/estate")

    # Recent transfer
    try:
        if int(transfer[:4]) >= 2020:
            score += 1
            signals.append("recent transfer")
    except (ValueError, TypeError):
        pass

    # Low value
    try:
        if float(appr_val) <= 150_000:
            score += 1
            signals.append("low value")
    except (ValueError, TypeError):
        pass

    # Vacant / no structure
    try:
        if float(bld_val) == 0:
            score += 1
            signals.append("vacant/no structure")
    except (ValueError, TypeError):
        if not bld_val or str(bld_val).strip() in ("", "nan", "None"):
            score += 1
            signals.append("vacant/no structure")

    return min(score, 10), " | ".join(signals)


def _format_mail_name(owner: str) -> str:
    """Format owner name for direct mail — title case, strip excess."""
    if not owner:
        return ""
    return owner.strip().title()


def _format_mail_address(row: pd.Series) -> str:
    """Pull property address for mail merge."""
    return str(row.get("property_address", "") or "").strip()


def run(leads_path: str,
        priority_path: str,
        foreclosure_path: str,
        output_path: str,
        min_score: int) -> int:

    print()
    logger.info("=" * 56)
    logger.info("Phase 5 — Final Lead Aggregator")
    logger.info("=" * 56)

    # ── Load all available data ───────────────────────────────────────────────
    logger.info("\nLoading input files…")
    base        = _load_if_exists(leads_path)
    priority    = _load_if_exists(priority_path)
    foreclosure = _load_if_exists(foreclosure_path)

    if base.empty:
        raise FileNotFoundError(f"Required file not found: {leads_path}")

    # ── Build lookup sets from phase 3 and 4 outputs ─────────────────────────
    delinquent_accts: set = set()
    if not priority.empty and "acct_num" in priority.columns:
        td_col = "tax_delinquent"
        if td_col in priority.columns:
            delinquent_accts = set(
                priority.loc[priority[td_col].fillna("").str.upper().str.startswith("YES"), "acct_num"]
            )
        else:
            delinquent_accts = set(priority["acct_num"].dropna())
    logger.info("  Tax delinquent accounts loaded: %d", len(delinquent_accts))

    foreclosure_accts: set = set()
    if not foreclosure.empty and "acct_num" in foreclosure.columns:
        fc_col = "foreclosure_filing"
        if fc_col in foreclosure.columns:
            foreclosure_accts = set(
                foreclosure.loc[foreclosure[fc_col].fillna("").str.upper().str.startswith("YES"), "acct_num"]
            )
        else:
            foreclosure_accts = set(foreclosure["acct_num"].dropna())
    logger.info("  Foreclosure accounts loaded: %d", len(foreclosure_accts))

    # Merge tax_delinquent and foreclosure_filing columns into base if available
    if not priority.empty and "tax_delinquent" in priority.columns:
        td_map = priority.set_index("acct_num")["tax_delinquent"].to_dict()
        base["tax_delinquent"] = base["acct_num"].map(td_map).fillna("")

    if not foreclosure.empty and "foreclosure_filing" in foreclosure.columns:
        fc_map = foreclosure.set_index("acct_num")["foreclosure_filing"].to_dict()
        base["foreclosure_filing"] = base["acct_num"].map(fc_map).fillna("")

    # Convert numeric columns
    for col in ("appraised_value", "market_value", "land_value", "building_value", "land_sqft"):
        if col in base.columns:
            base[col] = pd.to_numeric(base[col], errors="coerce")

    # ── Score every row ───────────────────────────────────────────────────────
    logger.info("\nScoring %d leads…", len(base))
    results = base.apply(
        lambda row: score_and_label(row, delinquent_accts, foreclosure_accts),
        axis=1,
        result_type="expand",
    )
    base["final_score"]   = results[0]
    base["final_signals"] = results[1]

    # ── Mail merge columns ────────────────────────────────────────────────────
    base["mail_name"]    = base["owner_name"].apply(_format_mail_name)
    base["mail_address"] = base.apply(_format_mail_address, axis=1)

    # ── Filter and sort ───────────────────────────────────────────────────────
    final = base[base["final_score"] >= min_score].copy()
    final = final.sort_values(
        ["final_score", "appraised_value"],
        ascending=[False, True],
    ).reset_index(drop=True)
    final.insert(0, "rank", range(1, len(final) + 1))

    final.to_csv(output_path, index=False)

    # ── Summary ───────────────────────────────────────────────────────────────
    top10 = final.head(10)[["rank", "owner_name", "property_address", "appraised_value", "final_score", "final_signals"]]

    print()
    print("=" * 56)
    print("  PHASE 5 COMPLETE — FINAL LEAD LIST")
    print("=" * 56)
    print(f"  Total leads scored : {len(base)}")
    print(f"  Leads in output    : {len(final)}  (score >= {min_score})")
    print(f"  Tax delinquent     : {len(delinquent_accts)} accounts")
    print(f"  Foreclosure filed  : {len(foreclosure_accts)} accounts")
    print(f"  Output file        : {output_path}")
    print()
    print("  TOP 10 LEADS:")
    print(top10.to_string(index=False))
    print("=" * 56)
    print()

    return len(final)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 5 — Final Lead Aggregator")
    parser.add_argument("--leads",       default="leads.csv",             help="Phase 2 leads (default: leads.csv)")
    parser.add_argument("--priority",    default="priority_leads.csv",    help="Phase 3 tax delinquent (default: priority_leads.csv)")
    parser.add_argument("--foreclosure", default="foreclosure_leads.csv", help="Phase 4 foreclosure (default: foreclosure_leads.csv)")
    parser.add_argument("--out",         default="final_leads.csv",       help="Output file (default: final_leads.csv)")
    parser.add_argument("--min-score",   default=3, type=int,             help="Min score to include (default: 3)")
    args = parser.parse_args()

    run(args.leads, args.priority, args.foreclosure, args.out, args.min_score)
