#!/usr/bin/env python3
"""
hcad_api_client.py

Harris County Appraisal District — ArcGIS REST API Client
Phase 1: Single property lookup via Harris County GIS Services

Endpoint:
  https://www.gis.hctx.net/arcgis/rest/services/HCAD/Parcels/MapServer/0/query

No authentication required.  No headless browser.  Pure JSON over HTTP GET.

Normalized output fields
------------------------
  hcad_num              HCAD parcel identifier
  acct_num              Appraisal account number (APN)
  owner_name            Current owner of record
  mailing_address       Full formatted mailing address of owner
  property_address      Site address of the parcel
  site_zip              Site zip code
  appraised_value       Total appraised value (land + building)
  land_value            Land-only value
  building_value        Improvement / building value
  transfer_date         Most recent ownership-transfer date (new_owner_date)

Usage
-----
  from hcad_api_client import HarrisCountyGISClient

  client = HarrisCountyGISClient()

  # By street name (returns all matching parcels on that street)
  records = client.query_property(street_name="KOWIS")

  # By street number + name
  records = client.query_property(street_num="5930", street_name="KOWIS")

  # By APN / account number
  records = client.query_property(account_num="0411310060006")

  for rec in records:
      client.pretty_print(rec)
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, List, Optional

import requests

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
ARCGIS_ENDPOINT = (
    "https://www.gis.hctx.net/arcgis/rest/services/HCAD/Parcels/MapServer/0/query"
)

# All fields published by the HCAD Parcels layer that we care about
OUT_FIELDS = ",".join([
    "HCAD_NUM",
    "acct_num",
    "owner_name_1",
    "site_str_num",
    "site_str_name",
    "site_str_sfx",     # street suffix (ST, AVE, BLVD …)
    "site_zip",
    "mail_addr_1",
    "mail_city",
    "mail_state",
    "mail_zip",
    "total_appraised_val",
    "land_value",
    "bld_value",
    "new_owner_date",
])

MAX_RESULTS  = 1000  # ArcGIS default page limit
MAX_RETRIES  = 4
RETRY_CODES  = {429, 500, 502, 503, 504}

# Rotate through real browser UA strings to reduce fingerprinting risk
USER_AGENTS: List[str] = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
        "Gecko/20100101 Firefox/125.0"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.4.1 Safari/605.1.15"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
]

# ── Normalized record type ────────────────────────────────────────────────────
PropertyRecord = Dict[str, Any]   # typed alias for clarity


# ── Client ────────────────────────────────────────────────────────────────────
class HarrisCountyGISClient:
    """
    Thin client for the Harris County HCAD Parcels ArcGIS MapServer.

    All queries go through a single `query_property()` method that builds
    the ArcGIS SQL `where` clause dynamically and returns a list of
    normalized PropertyRecord dicts.
    """

    def __init__(self) -> None:
        self.session = requests.Session()
        self._rotate_ua()

    # ── Session helpers ───────────────────────────────────────────────────────

    def _rotate_ua(self) -> None:
        """Swap in a random browser User-Agent + standard browser headers."""
        self.session.headers.update({
            "User-Agent":      random.choice(USER_AGENTS),
            "Accept":          "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection":      "keep-alive",
            "Referer":         "https://www.gis.hctx.net/",
        })

    def _get(self, url: str, params: Dict) -> requests.Response:
        """HTTP GET with exponential back-off on 429 / 5xx."""
        for attempt in range(1, MAX_RETRIES + 1):
            self._rotate_ua()
            try:
                resp = self.session.get(url, params=params, timeout=25)
                if resp.status_code in RETRY_CODES:
                    wait = 2 ** attempt
                    logger.warning(
                        "HTTP %d — retry %d/%d in %ds",
                        resp.status_code, attempt, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                    continue
                return resp
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES:
                    raise
                wait = 2 ** attempt
                logger.warning(
                    "Request error (%s) — retry %d/%d in %ds",
                    exc, attempt, MAX_RETRIES, wait,
                )
                time.sleep(wait)

        raise requests.RequestException(
            f"All {MAX_RETRIES} retries exhausted for {url}"
        )

    # ── WHERE clause builder ──────────────────────────────────────────────────

    @staticmethod
    def _build_where(
        street_num:  Optional[str],
        street_name: Optional[str],
        account_num: Optional[str],
        site_zip:    Optional[str],
    ) -> str:
        """
        Compose an ArcGIS-compatible SQL WHERE clause from any combination of
        the supplied parameters.  At least one parameter must be provided.

        Field contract (Harris County HCAD Parcels layer):
          acct_num       — 13-digit appraisal account / APN
          HCAD_NUM       — same value, alternate column name
          site_str_num   — house / street number (stored as string)
          site_str_name  — street name, uppercase, no suffix
          site_zip       — 5-digit zip code string
        """
        clauses: List[str] = []

        if account_num:
            acct = account_num.strip().lstrip("0")   # normalise leading zeros
            # Match against both column names in the layer
            clauses.append(
                f"(acct_num = '{account_num.strip()}' OR HCAD_NUM = '{account_num.strip()}')"
            )

        if street_name:
            name = street_name.strip().upper()
            # Use LIKE so partial names ("KOWIS") still match
            clauses.append(f"site_str_name LIKE '%{name}%'")

        if street_num:
            num = street_num.strip()
            clauses.append(f"site_str_num = '{num}'")

        if site_zip:
            clauses.append(f"site_zip = '{site_zip.strip()}'")

        if not clauses:
            raise ValueError(
                "At least one of street_num, street_name, account_num, or site_zip must be provided."
            )

        return " AND ".join(clauses)

    # ── Main lookup ───────────────────────────────────────────────────────────

    def query_property(
        self,
        street_num:  Optional[str] = None,
        street_name: Optional[str] = None,
        account_num: Optional[str] = None,
        site_zip:    Optional[str] = None,
        max_results: int = MAX_RESULTS,
    ) -> List[PropertyRecord]:
        """
        Query the HCAD Parcels ArcGIS layer and return normalized records.

        Parameters
        ----------
        street_num  : House / building number (e.g., "5930")
        street_name : Street name without suffix (e.g., "KOWIS").
                      Case-insensitive; partial matches accepted.
        account_num : 13-digit HCAD account number / APN.
        site_zip    : 5-digit zip code (e.g., "77028").
        max_results : Maximum number of features to return (default 1000).

        Returns
        -------
        List of PropertyRecord dicts, one per matching parcel.
        Empty list if nothing matched or if the server is unreachable.
        """
        where = self._build_where(street_num, street_name, account_num, site_zip)
        logger.info("ArcGIS WHERE clause: %s", where)

        params: Dict[str, Any] = {
            "where":          where,
            "outFields":      OUT_FIELDS,
            "returnGeometry": "false",
            "resultRecordCount": max_results,
            "f":              "json",
        }

        try:
            resp = self._get(ARCGIS_ENDPOINT, params)
        except requests.RequestException as exc:
            logger.error("Network error querying ArcGIS: %s", exc)
            return []

        if resp.status_code != 200:
            logger.error(
                "ArcGIS returned HTTP %d — body: %s",
                resp.status_code,
                resp.text[:300],
            )
            return []

        try:
            payload = resp.json()
        except ValueError:
            logger.error("Non-JSON response from ArcGIS: %s", resp.text[:300])
            return []

        # ArcGIS wraps errors in a top-level "error" key
        if "error" in payload:
            err = payload["error"]
            logger.error(
                "ArcGIS API error %s: %s",
                err.get("code"), err.get("message"),
            )
            return []

        features = payload.get("features", [])
        logger.info("ArcGIS returned %d feature(s)", len(features))

        records = [self._normalize(f["attributes"]) for f in features if "attributes" in f]
        return records

    # ── Field normalization ───────────────────────────────────────────────────

    @staticmethod
    def _normalize(attrs: Dict[str, Any]) -> PropertyRecord:
        """
        Map raw ArcGIS attribute dict → clean PropertyRecord dict.

        Handles None values, numeric types returned as floats, and
        assembles multi-part address strings.
        """

        def safe_str(v: Any) -> str:
            return str(v).strip() if v is not None else ""

        def safe_money(v: Any) -> Optional[float]:
            if v is None:
                return None
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        # ── Property address ─────────────────────────────────────────────────
        street_parts = filter(None, [
            safe_str(attrs.get("site_str_num")),
            safe_str(attrs.get("site_str_name")),
            safe_str(attrs.get("site_str_sfx")),
        ])
        property_address = " ".join(street_parts).strip()
        if property_address:
            property_address += f", Houston, TX {safe_str(attrs.get('site_zip'))}".rstrip(", TX ")

        # ── Mailing address ──────────────────────────────────────────────────
        mailing_parts = filter(None, [
            safe_str(attrs.get("mail_addr_1")),
            safe_str(attrs.get("mail_city")),
            safe_str(attrs.get("mail_state")),
            safe_str(attrs.get("mail_zip")),
        ])
        mailing_address = ", ".join(mailing_parts).strip()

        return {
            "hcad_num":         safe_str(attrs.get("HCAD_NUM")),
            "acct_num":         safe_str(attrs.get("acct_num")),
            "owner_name":       safe_str(attrs.get("owner_name_1")),
            "mailing_address":  mailing_address,
            "property_address": property_address,
            "site_zip":         safe_str(attrs.get("site_zip")),
            "appraised_value":  safe_money(attrs.get("total_appraised_val")),
            "land_value":       safe_money(attrs.get("land_value")),
            "building_value":   safe_money(attrs.get("bld_value")),
            "transfer_date":    safe_str(attrs.get("new_owner_date")),
        }

    # ── Display helper ────────────────────────────────────────────────────────

    @staticmethod
    def pretty_print(record: PropertyRecord) -> None:
        """Print a single PropertyRecord in a clean, human-readable format."""
        w = 56
        print()
        print("=" * w)
        print("  HARRIS COUNTY PROPERTY RECORD  (HCAD ArcGIS)")
        print("=" * w)

        def fmt_money(v: Optional[float]) -> str:
            return f"${v:,.0f}" if v is not None else "N/A"

        rows = [
            ("HCAD Parcel #",    record.get("hcad_num")         or "N/A"),
            ("Account # (APN)",  record.get("acct_num")         or "N/A"),
            ("Owner Name",       record.get("owner_name")       or "N/A"),
            ("Owner Mailing",    record.get("mailing_address")  or "N/A"),
            ("Property Address", record.get("property_address") or "N/A"),
            ("Site Zip",         record.get("site_zip")         or "N/A"),
            ("Appraised Value",  fmt_money(record.get("appraised_value"))),
            ("  Land Value",     fmt_money(record.get("land_value"))),
            ("  Building Value", fmt_money(record.get("building_value"))),
            ("Transfer Date",    record.get("transfer_date")    or "N/A"),
        ]

        for label, value in rows:
            print(f"  {label:<22} {value}")

        print("=" * w)
        print()


# ── Standalone execution / quick verification ─────────────────────────────────
if __name__ == "__main__":
    import json as _json
    import sys as _sys

    print("\nHCAD ArcGIS Client — Phase 1 Verification")
    print("Test address: Kowis St, Houston, TX 77028\n")

    client = HarrisCountyGISClient()

    # Query by street name + zip — matches all parcels on Kowis St in 77028
    records = client.query_property(street_name="KOWIS", site_zip="77028")

    if not records:
        print(
            "[WARN] No records returned.\n"
            "  If you see 'Host not in allowlist' above, Harris County's\n"
            "  ArcGIS server is blocking this machine's IP address.\n"
            "  Run from a local / residential internet connection to get live data.\n"
        )
        _sys.exit(0)

    print(f"  {len(records)} parcel(s) found on Kowis St (77028)\n")

    for rec in records:
        client.pretty_print(rec)

    # Also dump the raw dicts so every field is visible
    print("Raw JSON output:")
    print(_json.dumps(records, indent=2, default=str))
