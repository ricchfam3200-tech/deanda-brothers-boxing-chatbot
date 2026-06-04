#!/usr/bin/env python3
"""
propstream_search.py

PropStream Property Search — North Houston Lead Engine
Targets: Studewood, 5th Ward, Northline, Aldine

Three search setups:
  Setup 1 — "Tired Landlord"    : SFR, individual owner, 10+ yrs, absentee, 50-100% equity
  Setup 2 — Vacant Land         : Land, vacant, 5+ yrs ownership, 90-100% equity
  Setup 3 — Pre-Foreclosure/Liens: Pre-foreclosure or liens $2,000+, highest motivation

Usage
-----
  # All three setups, all north Houston zones:
  python propstream_search.py

  # Single setup:
  python propstream_search.py --setup 1
  python propstream_search.py --setup 2
  python propstream_search.py --setup 3

  # Limit results per zip:
  python propstream_search.py --limit 25

  # Save combined output:
  python propstream_search.py --out north_houston_leads.csv

Credentials
-----------
  Set environment variables before running:
    PROPSTREAM_EMAIL=your@email.com
    PROPSTREAM_PASSWORD=yourpassword

  Or pass via CLI:
    python propstream_search.py --email you@email.com --password secret
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
logger = logging.getLogger(__name__)

# ── PropStream API ─────────────────────────────────────────────────────────────
PS_BASE      = "https://api.propstream.com"
PS_LOGIN     = f"{PS_BASE}/account/login"
PS_SEARCH    = f"{PS_BASE}/properties/search"
PS_EXPORT    = f"{PS_BASE}/properties/export"

HEADERS = {
    "Content-Type":  "application/json",
    "Accept":        "application/json",
    "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PropStreamClient/1.0",
}

MAX_PAGE_SIZE = 50   # PropStream max per page
MAX_PAGES     = 4    # pull up to 200 records per search config
MAX_RETRIES   = 4

# ── North Houston Target Zones ─────────────────────────────────────────────────
#
# Zone                Zip Codes
# -----------------   ----------------------------------------
# Studewood           77008, 77018  (Northside Village / Heights corridor)
# 5th Ward            77020, 77026
# Northline           77022, 77093
# Aldine              77032, 77037, 77038, 77039, 77060, 77073
#
NORTH_HOUSTON_ZONES: Dict[str, List[str]] = {
    "Studewood":  ["77008", "77018"],
    "5th Ward":   ["77020", "77026"],
    "Northline":  ["77022", "77093"],
    "Aldine":     ["77032", "77037", "77038", "77039", "77060", "77073"],
}

ALL_TARGET_ZIPS: List[str] = [
    z for zips in NORTH_HOUSTON_ZONES.values() for z in zips
]

# ── Search Setup Definitions ───────────────────────────────────────────────────

SETUP_1_TIRED_LANDLORD = {
    "name":        "Tired Landlord",
    "description": "SFR · Individual owner · 10+ yrs · Absentee · 50-100% equity",
    "filters": {
        "propertyTypes":    ["SFR"],           # Single-Family Residential
        "ownerType":        "Individual",      # No corporations/LLCs
        "yearsOwnedMin":    10,                # Long-term hold = more fatigue
        "occupancyStatus":  "Absentee",        # Not living there → rental or vacant
        "equityPercentMin": 50,                # 50% equity floor
        "equityPercentMax": 100,
    },
    "sort": {"field": "yearsOwned", "direction": "desc"},
    "output_prefix": "tired_landlord",
}

SETUP_2_VACANT_LAND = {
    "name":        "Vacant Land",
    "description": "Vacant land · 5+ yrs ownership · 90-100% equity (free & clear)",
    "filters": {
        "propertyTypes":    ["LAND", "VAC"],   # Vacant land / unimproved
        "occupancyStatus":  "Vacant",
        "yearsOwnedMin":    5,
        "equityPercentMin": 90,
        "equityPercentMax": 100,
    },
    "sort": {"field": "equityPercent", "direction": "desc"},
    "output_prefix": "vacant_land",
}

SETUP_3_PRE_FORECLOSURE = {
    "name":        "Pre-Foreclosure / Liens",
    "description": "Pre-foreclosure or liens ≥ $2,000 · Highest motivation",
    "filters": {
        "preForeclosure":  True,
        "lienAmountMin":   2000,   # Substantial lien = real incentive to sell
    },
    "sort": {"field": "lienAmount", "direction": "desc"},
    "output_prefix": "pre_foreclosure_liens",
}

ALL_SETUPS = [SETUP_1_TIRED_LANDLORD, SETUP_2_VACANT_LAND, SETUP_3_PRE_FORECLOSURE]

# ── Output columns ─────────────────────────────────────────────────────────────
OUTPUT_COLUMNS = [
    "rank", "setup", "zone",
    "owner_name", "mailing_address", "property_address",
    "property_type", "years_owned", "occupancy_status",
    "estimated_value", "equity_percent", "equity_amount",
    "lien_amount", "pre_foreclosure", "tax_delinquent",
    "acct_num", "pull_date",
]


# ═════════════════════════════════════════════════════════════════════════════
# PropStream API Client
# ═════════════════════════════════════════════════════════════════════════════

class PropStreamClient:
    """Thin authenticated client for the PropStream search API."""

    def __init__(self, email: str, password: str):
        self.email    = email
        self.password = password
        self.token: Optional[str] = None
        self.session  = requests.Session()
        self.session.headers.update(HEADERS)

    # ── Authentication ─────────────────────────────────────────────────────────

    def login(self) -> bool:
        payload = {"username": self.email, "password": self.password}
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.post(PS_LOGIN, json=payload, timeout=20)
                if resp.status_code == 200:
                    data = resp.json()
                    # PropStream returns {"accessToken": "...", "tokenType": "Bearer"}
                    token = (
                        data.get("accessToken")
                        or data.get("access_token")
                        or data.get("token")
                    )
                    if token:
                        self.token = token
                        self.session.headers["Authorization"] = f"Bearer {token}"
                        logger.info("  PropStream login successful")
                        return True
                    logger.error("  Login response missing token: %s", data)
                    return False
                if resp.status_code in (401, 403):
                    logger.error("  Invalid PropStream credentials (HTTP %s)", resp.status_code)
                    return False
                # Transient error — retry
                logger.warning("  Login HTTP %s — retry %d/%d", resp.status_code, attempt, MAX_RETRIES)
                time.sleep(2 ** attempt)
            except requests.RequestException as exc:
                logger.warning("  Login error: %s — retry %d/%d", exc, attempt, MAX_RETRIES)
                time.sleep(2 ** attempt)
        return False

    # ── Property Search ────────────────────────────────────────────────────────

    def search(
        self,
        zip_codes: List[str],
        filters: Dict[str, Any],
        sort:    Dict[str, str],
        page_size: int = MAX_PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> List[Dict]:
        """
        Search PropStream for properties matching filters in the given zip codes.
        Returns a flat list of normalized property dicts.
        """
        all_results: List[Dict] = []

        for page in range(1, max_pages + 1):
            body = self._build_request_body(zip_codes, filters, sort, page, page_size)
            records, total_pages = self._fetch_page(body, page)
            all_results.extend(records)

            if page >= total_pages:
                break
            time.sleep(0.8)  # polite rate limiting

        return all_results

    def _build_request_body(
        self,
        zip_codes: List[str],
        filters:   Dict[str, Any],
        sort:      Dict[str, str],
        page:      int,
        page_size: int,
    ) -> Dict:
        body: Dict[str, Any] = {
            "market": {
                "state":  "TX",
                "county": "Harris",
                "zips":   zip_codes,
            },
            "pagination": {
                "page":     page,
                "pageSize": page_size,
            },
            "sort": sort,
        }

        # Property type
        if "propertyTypes" in filters:
            body["property"] = {"propertyType": filters["propertyTypes"]}

        # Occupancy
        if "occupancyStatus" in filters:
            body.setdefault("property", {})["occupancyStatus"] = filters["occupancyStatus"]

        # Owner
        owner_block: Dict[str, Any] = {}
        if "ownerType" in filters:
            owner_block["ownerType"] = filters["ownerType"]
        if "yearsOwnedMin" in filters:
            owner_block["yearsOwned"] = {"min": filters["yearsOwnedMin"]}
        if owner_block:
            body["owner"] = owner_block

        # Equity
        if "equityPercentMin" in filters or "equityPercentMax" in filters:
            body["equity"] = {
                "estimatedEquityPercent": {
                    "min": filters.get("equityPercentMin", 0),
                    "max": filters.get("equityPercentMax", 100),
                }
            }

        # Pre-foreclosure / liens
        distress: Dict[str, Any] = {}
        if filters.get("preForeclosure"):
            distress["preForeclosure"] = True
        if "lienAmountMin" in filters:
            distress["lien"] = {"amount": {"min": filters["lienAmountMin"]}}
        if distress:
            body["distress"] = distress

        return body

    def _fetch_page(self, body: Dict, page: int) -> Tuple[List[Dict], int]:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.post(PS_SEARCH, json=body, timeout=30)
                if resp.status_code == 401:
                    logger.warning("  Token expired — re-authenticating...")
                    if self.login():
                        continue
                    return [], 1
                if resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt)
                    continue
                if resp.status_code != 200:
                    logger.error("  Search HTTP %s on page %d: %s",
                                 resp.status_code, page, resp.text[:200])
                    return [], 1

                data = resp.json()
                # PropStream response shape: {"properties": [...], "totalPages": N, "totalCount": N}
                properties = (
                    data.get("properties")
                    or data.get("results")
                    or data.get("data")
                    or []
                )
                total_pages = (
                    data.get("totalPages")
                    or data.get("total_pages")
                    or 1
                )
                return [_normalize_ps_record(p) for p in properties], int(total_pages)

            except requests.RequestException as exc:
                logger.warning("  Search error page %d: %s — retry %d/%d",
                               page, exc, attempt, MAX_RETRIES)
                time.sleep(2 ** attempt)

        return [], 1


# ═════════════════════════════════════════════════════════════════════════════
# Record Normalizer
# ═════════════════════════════════════════════════════════════════════════════

def _normalize_ps_record(rec: Dict) -> Dict:
    """Flatten a PropStream property record into our standard output schema."""

    def s(keys, fallback=""):
        for k in keys if isinstance(keys, list) else [keys]:
            v = rec.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return fallback

    def n(keys, fallback=None):
        for k in keys if isinstance(keys, list) else [keys]:
            v = rec.get(k)
            if v is not None:
                try:
                    return float(v)
                except (ValueError, TypeError):
                    pass
        return fallback

    owner = s(["ownerName", "owner_name", "owner1Name", "owner"])
    owner2 = s(["owner2Name", "coOwnerName", "owner_name_2"])
    if owner2:
        owner = f"{owner} / {owner2}"

    prop_addr = s(["propertyAddress", "property_address", "siteAddress", "address"])
    city      = s(["propertyCity", "city", "siteCity"], "Houston")
    state_    = s(["propertyState", "state"], "TX")
    prop_zip  = s(["propertyZip", "zip", "propertyZipCode"])
    if prop_addr and city:
        prop_addr = f"{prop_addr}, {city}, {state_} {prop_zip}".strip(", ")

    mail_addr = s(["mailingAddress", "mailing_address", "mailAddress"])
    mail_city  = s(["mailingCity", "mail_city"])
    mail_state = s(["mailingState", "mail_state"])
    mail_zip   = s(["mailingZip", "mail_zip"])
    if mail_addr and mail_city:
        mail_addr = f"{mail_addr}, {mail_city}, {mail_state} {mail_zip}".strip(", ")

    return {
        "owner_name":       owner,
        "mailing_address":  mail_addr,
        "property_address": prop_addr,
        "property_type":    s(["propertyType", "property_type", "landUse"]),
        "years_owned":      n(["yearsOwned", "years_owned"]),
        "occupancy_status": s(["occupancyStatus", "occupancy_status"]),
        "estimated_value":  n(["estimatedValue", "estimated_value", "avm", "marketValue"]),
        "equity_percent":   n(["equityPercent", "equity_percent", "estimatedEquityPercent"]),
        "equity_amount":    n(["equityAmount", "equity_amount", "estimatedEquityAmount"]),
        "lien_amount":      n(["lienAmount", "lien_amount", "totalLienAmount"]),
        "pre_foreclosure":  bool(rec.get("preForeclosure") or rec.get("pre_foreclosure") or False),
        "tax_delinquent":   bool(rec.get("taxDelinquent") or rec.get("tax_delinquent") or False),
        "acct_num":         s(["accountNumber", "acct_num", "apn", "parcelNumber"]),
        "pull_date":        str(date.today()),
    }


# ═════════════════════════════════════════════════════════════════════════════
# Lead Scorer (PropStream-specific)
# ═════════════════════════════════════════════════════════════════════════════

def score_ps_lead(row: pd.Series, setup_name: str) -> int:
    """
    Score a PropStream lead 0-10.
    Higher score → closer to a motivated seller willing to discount.
    """
    score = 0

    prop_type   = str(row.get("property_type", "") or "").upper()
    yrs         = row.get("years_owned")
    equity_pct  = row.get("equity_percent")
    equity_amt  = row.get("equity_amount")
    lien        = row.get("lien_amount")
    mail        = str(row.get("mailing_address", "") or "").upper()
    est_val     = row.get("estimated_value")

    # Setup-specific base bonus
    if setup_name == "Tired Landlord":
        if yrs is not None:
            if yrs >= 20:
                score += 3  # very long hold = very tired
            elif yrs >= 15:
                score += 2
            elif yrs >= 10:
                score += 1
        if equity_pct is not None:
            if equity_pct >= 80:
                score += 2
            elif equity_pct >= 60:
                score += 1

    elif setup_name == "Vacant Land":
        if equity_pct is not None and equity_pct >= 95:
            score += 3   # free & clear land
        elif equity_pct is not None and equity_pct >= 90:
            score += 2
        if yrs is not None and yrs >= 10:
            score += 2   # decade of holding = likely forgotten asset
        elif yrs is not None and yrs >= 5:
            score += 1

    elif setup_name == "Pre-Foreclosure / Liens":
        if lien is not None:
            if lien >= 20_000:
                score += 4   # serious financial pressure
            elif lien >= 10_000:
                score += 3
            elif lien >= 5_000:
                score += 2
            elif lien >= 2_000:
                score += 1
        if row.get("pre_foreclosure"):
            score += 2
        if row.get("tax_delinquent"):
            score += 2

    # Universal signals
    if mail and ", TX" not in mail and "TEXAS" not in mail and len(mail) > 5:
        score += 1   # absentee/out-of-state owner

    try:
        if float(est_val or 0) <= 100_000:
            score += 1   # distressed value range
    except (ValueError, TypeError):
        pass

    if row.get("pre_foreclosure") and setup_name != "Pre-Foreclosure / Liens":
        score += 1
    if row.get("tax_delinquent") and setup_name != "Pre-Foreclosure / Liens":
        score += 1

    return min(score, 10)


# ═════════════════════════════════════════════════════════════════════════════
# Main Runner
# ═════════════════════════════════════════════════════════════════════════════

def run_setup(
    client:    PropStreamClient,
    setup:     Dict,
    zip_codes: List[str],
    zone_name: str,
    limit:     int,
) -> pd.DataFrame:
    """Run one PropStream search setup for a zone and return scored DataFrame."""
    logger.info("  [%s] %s — %d zips", zone_name, setup["name"], len(zip_codes))

    records = client.search(
        zip_codes=zip_codes,
        filters=setup["filters"],
        sort=setup["sort"],
        page_size=min(limit, MAX_PAGE_SIZE),
        max_pages=max(1, limit // MAX_PAGE_SIZE),
    )

    if not records:
        logger.warning("  [%s] %s — no results", zone_name, setup["name"])
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["setup"] = setup["name"]
    df["zone"]  = zone_name

    # Score each lead
    df["ps_score"] = df.apply(
        lambda row: score_ps_lead(row, setup["name"]), axis=1
    )

    # Sort by score DESC, equity DESC
    df = df.sort_values(["ps_score", "equity_percent"], ascending=[False, False])
    df = df.head(limit).reset_index(drop=True)

    return df


def run(
    email:      str,
    password:   str,
    setup_ids:  List[int],
    limit:      int,
    output:     str,
    zones:      Dict[str, List[str]] = None,
) -> int:
    if zones is None:
        zones = NORTH_HOUSTON_ZONES

    print()
    print("=" * 60)
    print("  PROPSTREAM NORTH HOUSTON SEARCH")
    print("=" * 60)
    print(f"  Setups : {', '.join(str(s) for s in setup_ids)}")
    print(f"  Zones  : {', '.join(zones.keys())}")
    print(f"  Limit  : top {limit} per zone per setup")
    print()

    client = PropStreamClient(email, password)
    print("[1/3] Authenticating with PropStream...")
    if not client.login():
        print("\n  ERROR: Could not log in. Check PROPSTREAM_EMAIL / PROPSTREAM_PASSWORD.\n")
        return 0

    selected_setups = [ALL_SETUPS[i - 1] for i in setup_ids if 1 <= i <= 3]

    all_frames: List[pd.DataFrame] = []
    total_steps = len(selected_setups) * len(zones)
    step = 0

    print(f"[2/3] Running {len(selected_setups)} setup(s) × {len(zones)} zone(s)...")
    for setup in selected_setups:
        print(f"\n  ── {setup['name']} ({setup['description']}) ──")
        zone_frames: List[pd.DataFrame] = []

        for zone_name, zips in zones.items():
            step += 1
            print(f"  [{step}/{total_steps}] {zone_name} ({', '.join(zips)})", end="\r")
            df = run_setup(client, setup, zips, zone_name, limit)
            if not df.empty:
                zone_frames.append(df)
            time.sleep(1.0)  # courtesy delay between zone queries

        if zone_frames:
            combined = pd.concat(zone_frames, ignore_index=True)
            combined = combined.sort_values("ps_score", ascending=False)
            combined.insert(0, "rank", range(1, len(combined) + 1))

            setup_file = f"{setup['output_prefix']}_{date.today()}.csv"
            _save(combined, setup_file)
            print(f"\n  Saved {len(combined)} leads → {setup_file}")
            all_frames.append(combined)

    print(f"\n[3/3] Writing combined output...")
    if not all_frames:
        print("  No results found. Check credentials and zip code coverage.\n")
        return 0

    final = pd.concat(all_frames, ignore_index=True)
    final = final.sort_values("ps_score", ascending=False).reset_index(drop=True)
    final.insert(0, "rank", range(1, len(final) + 1))
    _save(final, output)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  DONE")
    print("=" * 60)
    print(f"  Total leads found  : {len(final)}")
    print(f"  Combined output    : {output}")
    print()

    if not final.empty:
        print("  YOUR TOP 10 PICKS:")
        print()
        for _, row in final.head(10).iterrows():
            val_str = _fmt_val(row.get("estimated_value"))
            eq_str  = f"{row.get('equity_percent', 0):.0f}%" if row.get("equity_percent") else "N/A"
            flags   = []
            if row.get("pre_foreclosure"):  flags.append("PRE-FORECLOSURE")
            if row.get("tax_delinquent"):   flags.append("TAX DELINQUENT")
            if row.get("lien_amount"):      flags.append(f"LIEN ${row['lien_amount']:,.0f}")
            flag_str = " | ".join(flags) if flags else "—"

            print(f"  #{int(row['rank'])}  [{row.get('zone','')}]  {row.get('owner_name','')}")
            print(f"      {row.get('property_address','')}")
            print(f"      Score: {row.get('ps_score','')}/10  |  Value: {val_str}  |  Equity: {eq_str}")
            print(f"      Setup: {row.get('setup','')}  |  Flags: {flag_str}")
            print()

    print("=" * 60)
    print(f"\n  Open {output} in Google Sheets — your north Houston leads are ready.\n")
    return len(final)


def _save(df: pd.DataFrame, path: str):
    cols = [c for c in OUTPUT_COLUMNS if c in df.columns]
    extra = [c for c in df.columns if c not in cols and c not in ("ps_score",)]
    df[cols + extra].to_csv(path, index=False)


def _fmt_val(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "N/A"


# ── CLI ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="PropStream North Houston Property Search — Studewood · 5th Ward · Northline · Aldine"
    )
    parser.add_argument(
        "--setup", type=int, choices=[1, 2, 3], action="append", dest="setups",
        metavar="N",
        help="Which setup(s) to run: 1=Tired Landlord, 2=Vacant Land, 3=Pre-Foreclosure. "
             "Repeat flag for multiple (default: all three).",
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Max leads to return per zone per setup (default: 50)",
    )
    parser.add_argument(
        "--out", default=f"propstream_north_houston_{date.today()}.csv",
        help="Combined output CSV file",
    )
    parser.add_argument("--email",    default=os.getenv("PROPSTREAM_EMAIL", ""),
                        help="PropStream account email (or set PROPSTREAM_EMAIL)")
    parser.add_argument("--password", default=os.getenv("PROPSTREAM_PASSWORD", ""),
                        help="PropStream password (or set PROPSTREAM_PASSWORD)")

    parser.add_argument(
        "--zone", choices=list(NORTH_HOUSTON_ZONES.keys()), action="append", dest="zones",
        metavar="ZONE",
        help="Limit to specific zone(s): Studewood, '5th Ward', Northline, Aldine. "
             "Repeat for multiple (default: all zones).",
    )

    args = parser.parse_args()

    if not args.email or not args.password:
        parser.error(
            "PropStream credentials required.\n"
            "  Set PROPSTREAM_EMAIL and PROPSTREAM_PASSWORD environment variables,\n"
            "  or pass --email / --password flags."
        )

    setup_ids   = args.setups or [1, 2, 3]
    target_zones = (
        {z: NORTH_HOUSTON_ZONES[z] for z in args.zones}
        if args.zones else NORTH_HOUSTON_ZONES
    )

    run(
        email=args.email,
        password=args.password,
        setup_ids=setup_ids,
        limit=args.limit,
        output=args.out,
        zones=target_zones,
    )
