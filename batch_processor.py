#!/usr/bin/env python3
"""
batch_processor.py

Phase 2 — Harris County HCAD Batch Property Processor

Two input modes:
  1. CSV   — a file with columns: street_name, street_num (optional), acct_num (optional)
  2. Zip   — one or more of the 10 target zip codes; queries by mail_zip

Output: a single deduplicated CSV sorted by appraised value (desc),
        with a scrape_date column added.

Usage
-----
  # Query all streets from a CSV file
  python batch_processor.py --csv sample_input.csv --out results.csv

  # Sweep one zip code
  python batch_processor.py --zip 77028 --out results.csv

  # Sweep all 10 target zip codes
  python batch_processor.py --zip all --out results.csv

  # Or import and use directly
  from batch_processor import HCADBatchProcessor
  from hcad_api_client import HarrisCountyGISClient
  processor = HCADBatchProcessor(HarrisCountyGISClient())
  processor.run_csv("sample_input.csv", "results.csv")
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import time
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

from hcad_api_client import HarrisCountyGISClient, PropertyRecord

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Target zip codes (Northeast Houston wholesale focus) ──────────────────────
TARGET_ZIPS = [
    "77016", "77020", "77021", "77022", "77026",
    "77028", "77029", "77050", "77078", "77093",
]

# ── Output CSV columns (in order) ────────────────────────────────────────────
OUTPUT_COLUMNS = [
    "hcad_num",
    "acct_num",
    "owner_name",
    "mailing_address",
    "property_address",
    "property_type",
    "appraised_value",
    "market_value",
    "land_value",
    "building_value",
    "land_sqft",
    "transfer_date",
    "source_query",
    "scrape_date",
]


# ── Batch processor ───────────────────────────────────────────────────────────
class HCADBatchProcessor:
    """
    Runs multiple HCAD queries and collects results into a single CSV.

    Parameters
    ----------
    client : HarrisCountyGISClient
        Configured API client (handles session, retries, rate limiting).
    delay_between_queries : float
        Extra pause in seconds added between each query on top of the
        client's built-in 2-3 second delay.  Default 0 (client delay only).
    """

    def __init__(
        self,
        client: Optional[HarrisCountyGISClient] = None,
        delay_between_queries: float = 0.0,
    ) -> None:
        self.client = client or HarrisCountyGISClient()
        self.extra_delay = delay_between_queries
        self._today = str(date.today())

    # ── Public runners ────────────────────────────────────────────────────────

    def run_csv(self, input_path: str, output_path: str) -> int:
        """
        Read a CSV file of queries and export all matching HCAD records.

        Input CSV columns (any combination works):
          street_name  — e.g. KOWIS
          street_num   — e.g. 5930  (optional, narrows to one address)
          acct_num     — 13-digit HCAD account number (optional)

        Returns the number of unique records written.
        """
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")

        queries = self._load_csv_queries(input_path)
        logger.info("Loaded %d queries from %s", len(queries), input_path)

        all_records: List[Dict] = []
        seen_accounts = set()

        for i, q in enumerate(queries, 1):
            label = q.get("street_name") or q.get("acct_num") or str(q)
            logger.info("[%d/%d] Querying: %s", i, len(queries), label)

            try:
                records = self.client.query_property(
                    street_name=q.get("street_name") or None,
                    street_num=q.get("street_num")   or None,
                    account_num=q.get("acct_num")    or None,
                )
            except Exception as exc:
                logger.warning("  Skipped — error: %s", exc)
                records = []

            new = 0
            for rec in records:
                key = rec.get("acct_num") or rec.get("hcad_num")
                if key and key in seen_accounts:
                    continue
                if key:
                    seen_accounts.add(key)
                rec["source_query"] = label
                rec["scrape_date"]  = self._today
                all_records.append(rec)
                new += 1

            logger.info("  → %d new record(s)  (total so far: %d)", new, len(all_records))

            if self.extra_delay:
                time.sleep(self.extra_delay)

        return self._write_output(all_records, output_path)

    def run_zip(self, zip_codes: List[str], output_path: str) -> int:
        """
        Fetch all HCAD records whose owner mailing address zip matches
        one of the supplied zip codes.

        Note: This filters by MAIL zip, not site zip (the layer has no
        site zip field).  For owner-occupied properties the mail zip equals
        the property zip.  Absentee owners whose mailing zip is outside the
        target area will be excluded — use run_csv() with a street list for
        full coverage of a zip code.

        Returns the number of unique records written.
        """
        all_records: List[Dict] = []
        seen_accounts = set()

        for i, z in enumerate(zip_codes, 1):
            logger.info("[%d/%d] Sweeping zip code %s …", i, len(zip_codes), z)
            records = self._query_by_mail_zip(z)
            new = 0
            for rec in records:
                key = rec.get("acct_num") or rec.get("hcad_num")
                if key and key in seen_accounts:
                    continue
                if key:
                    seen_accounts.add(key)
                rec["source_query"] = f"zip:{z}"
                rec["scrape_date"]  = self._today
                all_records.append(rec)
                new += 1
            logger.info("  → %d new record(s)  (total so far: %d)", new, len(all_records))

            if self.extra_delay:
                time.sleep(self.extra_delay)

        return self._write_output(all_records, output_path)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _query_by_mail_zip(self, zip_code: str) -> List[Dict]:
        """Query HCAD parcels layer by owner mailing zip code."""
        from hcad_api_client import ARCGIS_ENDPOINT, OUT_FIELDS, MAX_RESULTS

        params = {
            "where":             f"mail_zip LIKE '{zip_code}%'",
            "outFields":         OUT_FIELDS,
            "returnGeometry":    "false",
            "resultRecordCount": MAX_RESULTS,
            "f":                 "json",
        }
        try:
            resp = self.client._get(ARCGIS_ENDPOINT, params)
        except Exception as exc:
            logger.error("Network error for zip %s: %s", zip_code, exc)
            return []

        if resp.status_code != 200:
            logger.error("HTTP %d for zip %s: %s", resp.status_code, zip_code, resp.text[:200])
            return []

        try:
            payload = resp.json()
        except ValueError:
            logger.error("Non-JSON response for zip %s", zip_code)
            return []

        if "error" in payload:
            logger.error("ArcGIS error for zip %s: %s", zip_code, payload["error"])
            return []

        features = payload.get("features", [])
        logger.info("  ArcGIS returned %d feature(s) for zip %s", len(features), zip_code)
        return [self.client._normalize(f["attributes"]) for f in features if "attributes" in f]

    @staticmethod
    def _load_csv_queries(path: str) -> List[Dict]:
        """Read the input CSV and return a list of query dicts."""
        queries = []
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                # Normalise column names to lowercase/stripped
                clean = {k.strip().lower(): v.strip() for k, v in row.items() if v and v.strip()}
                if clean:
                    queries.append(clean)
        return queries

    def _write_output(self, records: List[Dict], output_path: str) -> int:
        """Write records to CSV, sorted by appraised_value descending."""
        if not records:
            logger.warning("No records to write.")
            return 0

        df = pd.DataFrame(records)

        # Add any missing columns
        for col in OUTPUT_COLUMNS:
            if col not in df.columns:
                df[col] = ""

        df = df[OUTPUT_COLUMNS]

        # Sort by appraised value (high to low)
        df["appraised_value"] = pd.to_numeric(df["appraised_value"], errors="coerce")
        df = df.sort_values("appraised_value", ascending=False).reset_index(drop=True)

        df.to_csv(output_path, index=False)
        logger.info("Wrote %d records → %s", len(df), output_path)
        print(f"\n  Saved {len(df)} records to: {output_path}\n")
        return len(df)


# ── CLI entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="HCAD Batch Processor — Phase 2"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--csv",
        metavar="INPUT.CSV",
        help="Path to input CSV with street_name / acct_num columns",
    )
    group.add_argument(
        "--zip",
        metavar="ZIP",
        help="Zip code to sweep (e.g. 77028) or 'all' for all 10 target zips",
    )
    parser.add_argument(
        "--out",
        metavar="OUTPUT.CSV",
        default="hcad_results.csv",
        help="Output CSV path (default: hcad_results.csv)",
    )
    args = parser.parse_args()

    processor = HCADBatchProcessor()

    if args.csv:
        total = processor.run_csv(args.csv, args.out)
    else:
        zips = TARGET_ZIPS if args.zip.lower() == "all" else [args.zip.strip()]
        total = processor.run_zip(zips, args.out)

    print(f"Done — {total} unique properties saved to {args.out}")
