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

# ── PropStream API ─────────────────────────────────────────────────────
PS_BASE      = "https://api.propstream.com"
PS_LOGIN     = f"{PS_BASE}/account/login"
PS_SEARCH    = f"{PS_BASE}/properties/search"
PS_EXPORT    = f"{PS_BASE}/properties/export"

HEADERS = {
    "Content-Type":  "application/json",
    "Accept":        "application/json",
    "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PropStreamClient/1.0",
}

MAX_PAGE_SIZE = 50
MAX_PAGES     = 4
MAX_RETRIES   = 4

# ── North Houston Target Zones ─────────────────────────────────────────────────────
NORTH_HOUSTON_ZONES: Dict[str, List[str]] = {
    "Studewood":          ["77008", "77018"],
    "5th Ward":           ["77020", "77026"],
    "Northline":          ["77022", "77093", "77076", "77088", "77016"],
    "Aldine":             ["77032", "77037", "77038", "77039", "77060", "77073"],
    "Greenspoint":        ["77067", "77090"],
    "Spring / FM 1960":   ["77373", "77388", "77389"],
    "Humble":             ["77338", "77339", "77346"],
    "Near Northside":     ["77009", "77091", "77092"],
}

ALL_TARGET_ZIPS: List[str] = [
    z for zips in NORTH_HOUSTON_ZONES.values() for z in zips
]

SETUP_1_TIRED_LANDLORD = {
    "name":        "Tired Landlord",
    "description": "SFR · Individual owner · 10+ yrs · Absentee · 50-100% equity",
    "filters": {
        "propertyTypes":    ["SFR"],
        "ownerType":        "Individual",
        "yearsOwnedMin":    10,
        "occupancyStatus":  "Absentee",
        "equityPercentMin": 50,
        "equityPercentMax": 100,
    },
    "sort": {"field": "yearsOwned", "direction": "desc"},
    "output_prefix": "tired_landlord",
}

SETUP_2_VACANT_LAND = {
    "name":        "Vacant Land",
    "description": "Vacant land · 5+ yrs ownership · 90-100% equity (free & clear)",
    "filters": {
        "propertyTypes":    ["LAND", "VAC"],
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
        "lienAmountMin":   2000,
    },
    "sort": {"field": "lienAmount", "direction": "desc"},
    "output_prefix": "pre_foreclosure_liens",
}

ALL_SETUPS = [SETUP_1_TIRED_LANDLORD, SETUP_2_VACANT_LAND, SETUP_3_PRE_FORECLOSURE]

OUTPUT_COLUMNS = [
    "rank", "setup", "zone",
    "owner_name", "mailing_address", "property_address",
    "property_type", "years_owned", "occupancy_status",
    "estimated_value", "equity_percent", "equity_amount",
    "lien_amount", "pre_foreclosure", "tax_delinquent",
    "acct_num", "pull_date",
]


class PropStreamClient:

    def __init__(self, email: str, password: str):
        self.email    = email
        self.password = password
        self.token: Optional[str] = None
        self.session  = requests.Session()
        self.session.headers.update(HEADERS)

    def login(self) -> bool:
        payload = {"username": self.email, "password": self.password}
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.post(PS_LOGIN, json=payload, timeout=20)
                if resp.status_code == 200:
                    data = resp.json()
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
                logger.warning("  Login HTTP %s — retry %d/%d", resp.status_code, attempt, MAX_RETRIES)
                time.sleep(2 ** attempt)
            except requests.RequestException as exc:
                logger.warning("  Login error: %s — retry %d/%d", exc, attempt, MAX_RETRIES)
                time.sleep(2 ** attempt)
        return False

    def search(self, zip_codes, filters, sort, page_size=MAX_PAGE_SIZE, max_pages=MAX_PAGES):
        all_results = []
        for page in range(1, max_pages + 1):
            body = self._build_request_body(zip_codes, filters, sort, page, page_size)
            records, total_pages = self._fetch_page(body, page)
            all_results.extend(records)
            if page >= total_pages:
                break
            time.sleep(0.8)
        return all_results

    def _build_request_body(self, zip_codes, filters, sort, page, page_size):
        body = {
            "market": {"state": "TX", "county": "Harris", "zips": zip_codes},
            "pagination": {"page": page, "pageSize": page_size},
            "sort": sort,
        }
        if "propertyTypes" in filters:
            body["property"] = {"propertyType": filters["propertyTypes"]}
        if "occupancyStatus" in filters:
            body.setdefault("property", {})["occupancyStatus"] = filters["occupancyStatus"]
        owner_block = {}
        if "ownerType" in filters:
            owner_block["ownerType"] = filters["ownerType"]
        if "yearsOwnedMin" in filters:
            owner_block["yearsOwned"] = {"min": filters["yearsOwnedMin"]}
        if owner_block:
            body["owner"] = owner_block
        if "equityPercentMin" in filters or "equityPercentMax" in filters:
            body["equity"] = {"estimatedEquityPercent": {"min": filters.get("equityPercentMin", 0), "max": filters.get("equityPercentMax", 100)}}
        distress = {}
        if filters.get("preForeclosure"):
            distress["preForeclosure"] = True
        if "lienAmountMin" in filters:
            distress["lien"] = {"amount": {"min": filters["lienAmountMin"]}}
        if distress:
            body["distress"] = distress
        return body

    def _fetch_page(self, body, page):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.post(PS_SEARCH, json=body, timeout=30)
                if resp.status_code == 401:
                    if self.login():
                        continue
                    return [], 1
                if resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt)
                    continue
                if resp.status_code != 200:
                    logger.error("  Search HTTP %s on page %d: %s", resp.status_code, page, resp.text[:200])
                    return [], 1
                data = resp.json()
                properties = data.get("properties") or data.get("results") or data.get("data") or []
                total_pages = data.get("totalPages") or data.get("total_pages") or 1
                return [_normalize_ps_record(p) for p in properties], int(total_pages)
            except requests.RequestException as exc:
                logger.warning("  Search error page %d: %s — retry %d/%d", page, exc, attempt, MAX_RETRIES)
                time.sleep(2 ** attempt)
        return [], 1


def _normalize_ps_record(rec):
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
    mail_addr  = s(["mailingAddress", "mailing_address", "mailAddress"])
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


def score_ps_lead(row, setup_name):
    score = 0
    yrs        = row.get("years_owned")
    equity_pct = row.get("equity_percent")
    lien       = row.get("lien_amount")
    mail       = str(row.get("mailing_address", "") or "").upper()
    est_val    = row.get("estimated_value")

    if setup_name == "Tired Landlord":
        if yrs is not None:
            if yrs >= 20:   score += 3
            elif yrs >= 15: score += 2
            elif yrs >= 10: score += 1
        if equity_pct is not None:
            if equity_pct >= 80:   score += 2
            elif equity_pct >= 60: score += 1
    elif setup_name == "Vacant Land":
        if equity_pct is not None:
            if equity_pct >= 95:   score += 3
            elif equity_pct >= 90: score += 2
        if yrs is not None:
            if yrs >= 10:  score += 2
            elif yrs >= 5: score += 1
    elif setup_name == "Pre-Foreclosure / Liens":
        if lien is not None:
            if lien >= 20_000:   score += 4
            elif lien >= 10_000: score += 3
            elif lien >= 5_000:  score += 2
            elif lien >= 2_000:  score += 1
        if row.get("pre_foreclosure"): score += 2
        if row.get("tax_delinquent"):  score += 2

    if mail and ", TX" not in mail and "TEXAS" not in mail and len(mail) > 5:
        score += 1
    try:
        if float(est_val or 0) <= 100_000:
            score += 1
    except (ValueError, TypeError):
        pass
    if row.get("pre_foreclosure") and setup_name != "Pre-Foreclosure / Liens": score += 1
    if row.get("tax_delinquent")  and setup_name != "Pre-Foreclosure / Liens": score += 1
    return min(score, 10)


def run_setup(client, setup, zip_codes, zone_name, limit):
    records = client.search(
        zip_codes=zip_codes,
        filters=setup["filters"],
        sort=setup["sort"],
        page_size=min(limit, MAX_PAGE_SIZE),
        max_pages=max(1, limit // MAX_PAGE_SIZE),
    )
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df["setup"]    = setup["name"]
    df["zone"]     = zone_name
    df["ps_score"] = df.apply(lambda row: score_ps_lead(row, setup["name"]), axis=1)
    df = df.sort_values(["ps_score", "equity_percent"], ascending=[False, False])
    return df.head(limit).reset_index(drop=True)


def run(email, password, setup_ids, limit, output, zones=None):
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
    all_frames = []
    total_steps = len(selected_setups) * len(zones)
    step = 0

    print(f"[2/3] Running {len(selected_setups)} setup(s) x {len(zones)} zone(s)...")
    for setup in selected_setups:
        print(f"\n  -- {setup['name']} ({setup['description']}) --")
        zone_frames = []
        for zone_name, zips in zones.items():
            step += 1
            print(f"  [{step}/{total_steps}] {zone_name} ({', '.join(zips)})", end="\r")
            df = run_setup(client, setup, zips, zone_name, limit)
            if not df.empty:
                zone_frames.append(df)
            time.sleep(1.0)
        if zone_frames:
            combined = pd.concat(zone_frames, ignore_index=True)
            combined = combined.sort_values("ps_score", ascending=False)
            combined.insert(0, "rank", range(1, len(combined) + 1))
            setup_file = f"{setup['output_prefix']}_{date.today()}.csv"
            _save(combined, setup_file)
            print(f"\n  Saved {len(combined)} leads -> {setup_file}")
            all_frames.append(combined)

    print("\n[3/3] Writing combined output...")
    if not all_frames:
        print("  No results found. Check credentials and zip code coverage.\n")
        return 0

    final = pd.concat(all_frames, ignore_index=True)
    final = final.sort_values("ps_score", ascending=False).reset_index(drop=True)
    final.insert(0, "rank", range(1, len(final) + 1))
    _save(final, output)

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
            if row.get("pre_foreclosure"): flags.append("PRE-FORECLOSURE")
            if row.get("tax_delinquent"):  flags.append("TAX DELINQUENT")
            if row.get("lien_amount"):     flags.append(f"LIEN ${row['lien_amount']:,.0f}")
            flag_str = " | ".join(flags) if flags else "--"
            print(f"  #{int(row['rank'])}  [{row.get('zone','')}]  {row.get('owner_name','')}")
            print(f"      {row.get('property_address','')}")
            print(f"      Score: {row.get('ps_score','')}/10  |  Value: {val_str}  |  Equity: {eq_str}")
            print(f"      Setup: {row.get('setup','')}  |  Flags: {flag_str}")
            print()

    print("=" * 60)
    print(f"\n  Open {output} in Google Sheets -- your north Houston leads are ready.\n")
    return len(final)


def _save(df, path):
    cols  = [c for c in OUTPUT_COLUMNS if c in df.columns]
    extra = [c for c in df.columns if c not in cols and c != "ps_score"]
    df[cols + extra].to_csv(path, index=False)


def _fmt_val(v):
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "N/A"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="PropStream North Houston Property Search"
    )
    parser.add_argument("--setup", type=int, choices=[1, 2, 3], action="append", dest="setups", metavar="N")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--out", default=f"propstream_north_houston_{date.today()}.csv")
    parser.add_argument("--email",    default=os.getenv("PROPSTREAM_EMAIL", ""))
    parser.add_argument("--password", default=os.getenv("PROPSTREAM_PASSWORD", ""))
    parser.add_argument("--zone", choices=list(NORTH_HOUSTON_ZONES.keys()), action="append", dest="zones", metavar="ZONE")
    args = parser.parse_args()

    if not args.email or not args.password:
        parser.error("PropStream credentials required.\n  Set PROPSTREAM_EMAIL and PROPSTREAM_PASSWORD env vars, or pass --email / --password.")

    setup_ids    = args.setups or [1, 2, 3]
    target_zones = {z: NORTH_HOUSTON_ZONES[z] for z in args.zones} if args.zones else NORTH_HOUSTON_ZONES

    run(email=args.email, password=args.password, setup_ids=setup_ids, limit=args.limit, output=args.out, zones=target_zones)
