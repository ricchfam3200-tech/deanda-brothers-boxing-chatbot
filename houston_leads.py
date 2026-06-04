#!/usr/bin/env python3
"""
houston_leads.py

Northeast Houston Wholesale Lead Engine
All phases in one command.

Pulls HCAD property data, filters for individual owners,
cross-references tax delinquent and distressed signals,
scores every lead, and outputs a ranked final_leads.csv.

Usage
-----
  # Query by street list (recommended):
  python houston_leads.py --streets sample_input.csv

  # Query a single zip code:
  python houston_leads.py --zip 77028

  # Query all 10 target zip codes:
  python houston_leads.py --zip all

  # Add LGBS tax sale list for delinquent cross-reference:
  python houston_leads.py --streets sample_input.csv --lgbs-file lgbs_harris.xlsx

  # Custom output file:
  python houston_leads.py --streets sample_input.csv --out my_leads.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import random
import re
import time
from datetime import date
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin

import pandas as pd
import requests

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
ARCGIS_ENDPOINT = (
    "https://www.gis.hctx.net/arcgis/rest/services/HCAD/Parcels/MapServer/0/query"
)

OUT_FIELDS = ",".join([
    "HCAD_NUM", "acct_num", "owner_name_1", "owner_name_2",
    "site_str_num", "site_str_pfx", "site_str_name", "site_str_sfx",
    "site_city", "StateClass", "mail_addr_1", "mail_addr_2",
    "mail_city", "mail_state", "mail_zip",
    "total_appraised_val", "total_market_val", "land_value",
    "impr_value", "new_owner_date", "land_sqft",
])

MAX_RESULTS = 1000
MAX_RETRIES = 4
RETRY_CODES = {429, 500, 502, 503, 504}

TARGET_ZIPS = [
    "77016", "77020", "77021", "77022", "77026",
    "77028", "77029", "77050", "77078", "77093",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

ACCT_PATTERN = re.compile(r"\b(\d{13})\b")

GOVERNMENT_KEYWORDS = [
    "HARRIS COUNTY", "HOUSTON CITY", "CITY OF HOUSTON", "HISD",
    "ALDINE ISD", "HUMBLE ISD", "SPRING ISD", "KATY ISD",
    "PASADENA ISD", "CYPRESS ISD", "STATE OF TEXAS", "TXDOT",
    "METRO", "PORT OF HOUSTON",
]

BUSINESS_KEYWORDS = [
    " LLC", " L.L.C", " INC", " INCORPORATED", " CORP", " CORPORATION",
    " LTD", " L.P.", " LP ", " LP,", " PARTNERS", " PARTNERSHIP",
    " HOLDINGS", " HOLDING", " PROPERTIES", " PROPERTY",
    " INVESTMENTS", " INVESTMENT", " REALTY", " REAL ESTATE",
    " GROUP", " COMPANY", " CO.", " MANAGEMENT", " ENTERPRISES",
    " ENTERPRISE", " SERVICES", " SERVICE", " SOLUTIONS", " VENTURES",
    " CAPITAL", " ACQUISITIONS", " ACQUISITION", " FUNDING",
    " ASSETS", " ASSET", " EQUITY", " BANK", " BANKING",
    " FINANCIAL", " FINANCE", " MORTGAGE", " LENDING", " LOAN",
    " CREDIT", " TRUST SERV", " TRUSTEE", " CHURCH", " MINISTRY",
    " HOUSING", " DEVELOPMENT",
]

BANK_KEYWORDS = [
    "TRUSTEE", "BANK", "BANCORP", "FINANCIAL", "MORTGAGE", "LENDING",
    "SERVICER", "FANNIE MAE", "FREDDIE MAC", "WELLS FARGO", "CHASE",
    "CITIBANK", "NATIONSTAR", "OCWEN", "NEWREZ", "MR COOPER",
    "PENNYMAC", "CALIBER HOME", "FREEDOM MORTGAGE", "CARRINGTON",
]

OUTPUT_COLUMNS = [
    "rank", "final_score", "final_signals",
    "owner_name", "mailing_address", "property_address",
    "property_type", "appraised_value", "market_value",
    "land_value", "building_value", "land_sqft",
    "transfer_date", "hcad_num", "acct_num",
    "mail_name", "scrape_date",
]


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 — HCAD DATA PULL
# ═════════════════════════════════════════════════════════════════════════════

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
        "Referer":         "https://www.gis.hctx.net/",
    })
    return s


def _get(session: requests.Session, url: str, params: dict) -> Optional[requests.Response]:
    for attempt in range(1, MAX_RETRIES + 1):
        session.headers["User-Agent"] = random.choice(USER_AGENTS)
        try:
            resp = session.get(url, params=params, timeout=25)
            if resp.status_code in RETRY_CODES:
                time.sleep(2 ** attempt)
                continue
            return resp
        except requests.RequestException:
            if attempt == MAX_RETRIES:
                return None
            time.sleep(2 ** attempt)
    return None


def _normalize(attrs: Dict[str, Any]) -> Dict:
    def s(v): return str(v).strip() if v is not None else ""
    def m(v):
        try: return float(v)
        except: return None

    addr_parts = filter(None, [s(attrs.get("site_str_num")), s(attrs.get("site_str_pfx")),
                                s(attrs.get("site_str_name")), s(attrs.get("site_str_sfx"))])
    city = s(attrs.get("site_city")) or "HOUSTON"
    prop_addr = " ".join(addr_parts).strip()
    if prop_addr:
        prop_addr = f"{prop_addr}, {city}, TX"

    owner = s(attrs.get("owner_name_1"))
    owner2 = s(attrs.get("owner_name_2"))
    if owner2:
        owner = f"{owner} / {owner2}"

    mail_parts = filter(None, [s(attrs.get("mail_addr_1")), s(attrs.get("mail_addr_2")),
                                s(attrs.get("mail_city")), s(attrs.get("mail_state")),
                                s(attrs.get("mail_zip"))])
    mail_addr = ", ".join(mail_parts).strip()

    transfer_date = ""
    raw = attrs.get("new_owner_date")
    if raw:
        try:
            import datetime
            transfer_date = datetime.datetime.utcfromtimestamp(int(raw) / 1000).strftime("%Y-%m-%d")
        except Exception:
            transfer_date = s(raw)

    return {
        "hcad_num":         s(attrs.get("HCAD_NUM")),
        "acct_num":         s(attrs.get("acct_num")),
        "owner_name":       owner,
        "mailing_address":  mail_addr,
        "property_address": prop_addr,
        "property_type":    s(attrs.get("StateClass")),
        "appraised_value":  m(attrs.get("total_appraised_val")),
        "market_value":     m(attrs.get("total_market_val")),
        "land_value":       m(attrs.get("land_value")),
        "building_value":   m(attrs.get("impr_value")),
        "land_sqft":        m(attrs.get("land_sqft")),
        "transfer_date":    transfer_date,
        "scrape_date":      str(date.today()),
    }


def pull_by_street(session, street_name: str, street_num: str = None) -> List[Dict]:
    where = f"site_str_name = '{street_name.strip().upper()}'"
    if street_num:
        where += f" AND site_str_num = '{street_num.strip()}'"
    params = {"where": where, "outFields": OUT_FIELDS,
              "returnGeometry": "false", "resultRecordCount": MAX_RESULTS, "f": "json"}
    resp = _get(session, ARCGIS_ENDPOINT, params)
    if not resp or resp.status_code != 200:
        return []
    try:
        payload = resp.json()
    except Exception:
        return []
    if "error" in payload:
        return []
    return [_normalize(f["attributes"]) for f in payload.get("features", []) if "attributes" in f]


def pull_by_zip(session, zip_code: str) -> List[Dict]:
    params = {"where": f"mail_zip LIKE '{zip_code}%'", "outFields": OUT_FIELDS,
              "returnGeometry": "false", "resultRecordCount": MAX_RESULTS, "f": "json"}
    resp = _get(session, ARCGIS_ENDPOINT, params)
    if not resp or resp.status_code != 200:
        return []
    try:
        payload = resp.json()
    except Exception:
        return []
    if "error" in payload:
        return []
    return [_normalize(f["attributes"]) for f in payload.get("features", []) if "attributes" in f]


def pull_hcad(streets_csv: str = None, zip_codes: List[str] = None) -> pd.DataFrame:
    session = _make_session()
    all_records: List[Dict] = []
    seen: Set[str] = set()

    def add(records, label):
        new = 0
        for rec in records:
            key = rec.get("acct_num") or rec.get("hcad_num")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            rec["source_query"] = label
            all_records.append(rec)
            new += 1
        return new

    if streets_csv:
        queries = []
        with open(streets_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                clean = {k.strip().lower(): v.strip() for k, v in row.items() if v and v.strip()}
                if clean:
                    queries.append(clean)

        print(f"\n  Pulling HCAD data for {len(queries)} streets...")
        for i, q in enumerate(queries, 1):
            label = q.get("street_name") or q.get("acct_num") or str(q)
            print(f"  [{i}/{len(queries)}] {label}", end="\r")
            records = pull_by_street(session, q.get("street_name", ""),
                                     q.get("street_num") or None)
            n = add(records, label)
            time.sleep(random.uniform(2, 3))
        print(f"\n  Pulled {len(all_records)} total properties")

    if zip_codes:
        print(f"\n  Pulling HCAD data for {len(zip_codes)} zip codes...")
        for i, z in enumerate(zip_codes, 1):
            print(f"  [{i}/{len(zip_codes)}] zip {z}", end="\r")
            records = pull_by_zip(session, z)
            n = add(records, f"zip:{z}")
            time.sleep(random.uniform(2, 3))
        print(f"\n  Pulled {len(all_records)} total properties")

    return pd.DataFrame(all_records)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 — OWNER TYPE DETECTION
# ═════════════════════════════════════════════════════════════════════════════

def is_government(owner: str) -> bool:
    up = owner.upper()
    return any(kw in up for kw in GOVERNMENT_KEYWORDS)


def is_individual(owner: str) -> bool:
    if not owner or not owner.strip():
        return False
    if is_government(owner):
        return False
    up = " " + owner.upper() + " "
    if "ESTATE OF" in up:
        return True
    return not any(kw in up for kw in BUSINESS_KEYWORDS)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 — TAX DELINQUENT (LGBS)
# ═════════════════════════════════════════════════════════════════════════════

def load_lgbs_file(path: str) -> Set[str]:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(path, dtype=str, engine="openpyxl")
        else:
            try:
                df = pd.read_csv(path, dtype=str, encoding="utf-8")
            except UnicodeDecodeError:
                df = pd.read_csv(path, dtype=str, encoding="latin-1")
        accounts = set(ACCT_PATTERN.findall(df.to_string()))
        logger.info("  LGBS file: %d delinquent accounts found", len(accounts))
        return accounts
    except Exception as exc:
        logger.warning("  Could not read LGBS file: %s", exc)
        return set()


def fetch_lgbs_auto() -> Set[str]:
    session = _make_session()
    session.headers.update({"Accept": "application/json, */*", "Referer": "https://taxsales.lgbs.com/"})
    accounts: Set[str] = set()

    api_urls = [
        "https://taxsales.lgbs.com/api/properties/?county=harris&limit=500&offset=0",
        "https://taxsales.lgbs.com/api/sales/?sale_county=HARRIS+COUNTY&limit=500",
        "https://taxsales.lgbs.com/properties/?format=json&county=harris&limit=500",
    ]

    for url in api_urls:
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    records = data if isinstance(data, list) else next(
                        (data[k] for k in ("results","properties","data","items") if k in data and isinstance(data[k], list)), []
                    )
                    for rec in records:
                        accounts.update(ACCT_PATTERN.findall(str(rec)))
                    if accounts:
                        return accounts
                    raw = set(ACCT_PATTERN.findall(resp.text))
                    if raw:
                        return raw
                except Exception:
                    raw = set(ACCT_PATTERN.findall(resp.text))
                    if raw:
                        return raw
        except Exception:
            pass
        time.sleep(1)

    return accounts


def get_delinquent_accounts(lgbs_file: str = None) -> Set[str]:
    if lgbs_file and os.path.exists(lgbs_file):
        print("  Loading LGBS file...")
        return load_lgbs_file(lgbs_file)
    print("  Trying LGBS auto-fetch...")
    accounts = fetch_lgbs_auto()
    if accounts:
        print(f"  LGBS: {len(accounts)} delinquent accounts found")
    else:
        print("  LGBS unavailable — tax delinquent signals skipped")
        print("  (Download lgbs_harris.xlsx from taxsales.lgbs.com and re-run with --lgbs-file)")
    return accounts


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4 — DISTRESS / FORECLOSURE SIGNALS
# ═════════════════════════════════════════════════════════════════════════════

def is_bank_owned(owner: str) -> bool:
    up = owner.upper()
    return any(kw in up for kw in BANK_KEYWORDS)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 — SCORING
# ═════════════════════════════════════════════════════════════════════════════

def score_lead(row: pd.Series, delinquent_accts: Set[str]) -> tuple:
    score = 0
    signals = []

    owner     = str(row.get("owner_name", "") or "").upper()
    mail_addr = str(row.get("mailing_address", "") or "").upper()
    prop_type = str(row.get("property_type", "") or "").upper().strip()
    transfer  = str(row.get("transfer_date", "") or "")
    acct      = str(row.get("acct_num", "") or "").strip()
    appr_val  = row.get("appraised_value")
    bld_val   = row.get("building_value")
    land_val  = row.get("land_value")

    if prop_type == "A1":
        score += 3
        signals.append("single-family")

    if mail_addr and ", TX" not in mail_addr and "TEXAS" not in mail_addr:
        score += 2
        signals.append("out-of-state owner")

    if "ESTATE OF" in owner or "ESTATE" in owner:
        score += 2
        signals.append("probate/estate")

    if acct in delinquent_accts:
        score += 3
        signals.append("TAX DELINQUENT")

    try:
        if int(transfer[:4]) >= 2020:
            score += 1
            signals.append("recent transfer")
    except Exception:
        pass

    try:
        val = float(appr_val)
        if val <= 80_000:
            score += 2
            signals.append("low value <$80K")
        elif val <= 150_000:
            score += 1
            signals.append("low value <$150K")
    except Exception:
        pass

    try:
        if float(bld_val) == 0:
            score += 1
            signals.append("no structure")
    except Exception:
        if not bld_val or str(bld_val).strip() in ("", "nan", "None"):
            score += 1
            signals.append("no structure")

    try:
        lv = float(land_val) if land_val else 0
        av = float(appr_val) if appr_val else 0
        if av > 0 and lv / av >= 0.70 and av <= 100_000:
            score += 1
            signals.append("land-heavy/distressed")
    except Exception:
        pass

    return min(score, 10), " | ".join(signals)


# ═════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═════════════════════════════════════════════════════════════════════════════

def run(streets_csv: str, zip_arg: str, output_path: str,
        lgbs_file: str, min_score: int, from_csv: str = None) -> int:

    print()
    print("=" * 60)
    print("  HOUSTON WHOLESALE LEAD ENGINE")
    print("=" * 60)

    # ── 1. Load or pull HCAD data ─────────────────────────────────────────────
    if from_csv:
        print(f"\n[1/4] Loading existing data from {from_csv}...")
        if not os.path.exists(from_csv):
            print(f"\n  ERROR: File not found: {from_csv}\n")
            return 0
        df = pd.read_csv(from_csv, dtype=str, low_memory=False)
        print(f"  {len(df)} properties loaded")
    else:
        print("\n[1/4] Pulling HCAD property data...")
        zip_codes = None
        if zip_arg:
            zip_codes = TARGET_ZIPS if zip_arg.lower() == "all" else [zip_arg.strip()]

        df = pull_hcad(streets_csv=streets_csv, zip_codes=zip_codes)

        if df.empty:
            print("\n  No data returned from HCAD.")
            print("  Harris County GIS blocks some networks.")
            print("  Try running from a home internet connection.")
            print("\n  TIP: If you already have results.csv, run:")
            print("       python houston_leads.py --from-csv results.csv\n")
            return 0

        print(f"  {len(df)} total properties pulled")

    # ── 2. Filter: individuals only, remove government ────────────────────────
    print("\n[2/4] Filtering for individual owners...")
    df = df[~df["owner_name"].fillna("").apply(is_government)].copy()
    df = df[df["owner_name"].fillna("").apply(is_individual)].copy()
    print(f"  {len(df)} individual-owner properties remaining")

    # ── 3. Tax delinquent lookup ──────────────────────────────────────────────
    print("\n[3/4] Checking tax delinquent status...")
    delinquent_accts = get_delinquent_accounts(lgbs_file)

    # ── 4. Score and rank ─────────────────────────────────────────────────────
    print("\n[4/4] Scoring and ranking leads...")
    for col in ("appraised_value", "market_value", "land_value", "building_value", "land_sqft"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    results = df.apply(
        lambda row: score_lead(row, delinquent_accts), axis=1, result_type="expand"
    )
    df["final_score"]   = results[0]
    df["final_signals"] = results[1]
    df["mail_name"]     = df["owner_name"].str.title()

    final = df[df["final_score"] >= min_score].copy()
    final = final.sort_values(
        ["final_score", "appraised_value"], ascending=[False, True]
    ).reset_index(drop=True)
    final.insert(0, "rank", range(1, len(final) + 1))

    # Keep only output columns that exist
    cols = [c for c in OUTPUT_COLUMNS if c in final.columns]
    final = final[cols]

    final.to_csv(output_path, index=False)

    # ── Summary ───────────────────────────────────────────────────────────────
    delinquent_count = int((results[0] >= 3).sum()) if not results.empty else 0

    print()
    print("=" * 60)
    print("  DONE")
    print("=" * 60)
    print(f"  Individual owners found  : {len(df)}")
    print(f"  Leads (score >= {min_score})       : {len(final)}")
    delinquent_hits = len(final[final["final_signals"].str.contains("TAX DELINQUENT", na=False)]) if "final_signals" in final.columns else 0
    print(f"  Tax delinquent matched   : {delinquent_hits}")
    print(f"  Output file              : {output_path}")
    print()

    if not final.empty:
        print("  YOUR TOP 5 LEADS:")
        print()
        for _, row in final.head(5).iterrows():
            try:
                val = float(row.get("appraised_value") or 0)
                val_str = f"${val:,.0f}"
            except Exception:
                val_str = "N/A"
            print(f"  #{int(row['rank'])}  {row.get('owner_name','')}")
            print(f"      {row.get('property_address','')}")
            print(f"      Score: {row.get('final_score','')}/10  |  Value: {val_str}")
            print(f"      Signals: {row.get('final_signals','')}")
            print()

    print("=" * 60)
    print(f"\n  Opening {output_path}...\n")

    try:
        os.startfile(os.path.abspath(output_path))
    except Exception:
        print(f"  Could not auto-open file. Find it here:")
        print(f"  {os.path.abspath(output_path)}\n")

    return len(final)


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Houston Wholesale Lead Engine — pulls HCAD data and scores motivated sellers"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--streets",   metavar="CSV", help="CSV file with street_name column (e.g. sample_input.csv)")
    mode.add_argument("--zip",       metavar="ZIP", help="Zip code to sweep, e.g. 77028, or 'all' for all 10 target zips")
    mode.add_argument("--from-csv",  metavar="CSV", help="Use existing results.csv instead of pulling fresh HCAD data")

    parser.add_argument("--out",       default="final_leads.csv", help="Output file (default: final_leads.csv)")
    parser.add_argument("--lgbs-file", default="",                help="Path to downloaded LGBS Excel/CSV for tax delinquent cross-reference")
    parser.add_argument("--min-score", default=3, type=int,       help="Minimum lead score 0-10 (default: 3)")

    args = parser.parse_args()
    run(args.streets, args.zip, args.out, args.lgbs_file, args.min_score,
        from_csv=getattr(args, "from_csv", None))
