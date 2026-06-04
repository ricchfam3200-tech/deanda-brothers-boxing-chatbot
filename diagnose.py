#!/usr/bin/env python3
"""
diagnose.py

Quick diagnostic — checks what field names actually exist in the
Harris County HCAD ArcGIS layer and fetches 1 sample record.
Run this first if hcad_api_client.py returns 0 results.

Usage:
  python diagnose.py
"""

import json
import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
BASE = "https://www.gis.hctx.net/arcgis/rest/services/HCAD/Parcels/MapServer/0"
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

print("\n" + "=" * 56)
print("  HCAD ArcGIS Layer Diagnostic")
print("=" * 56)

# ── Step 1: Check layer metadata + real field names ───────────────────────────
print("\n[1] Fetching layer metadata to confirm field names...")
r = requests.get(BASE, params={"f": "json"}, headers=HEADERS, timeout=20)
print(f"    HTTP status: {r.status_code}")

if r.status_code == 200:
    meta = r.json()
    fields = meta.get("fields", [])
    print(f"    Layer name : {meta.get('name', 'N/A')}")
    print(f"    Total fields available: {len(fields)}")
    print("\n    Field names in this layer:")
    for f in fields:
        print(f"      {f['name']:<35} ({f['type']})")
else:
    print(f"    ERROR: {r.text[:200]}")
    raise SystemExit

# ── Step 2: Fetch 1 sample record to see real data values ─────────────────────
print("\n[2] Fetching 1 sample record (any record in zip 77028)...")
r2 = requests.get(
    BASE + "/query",
    params={
        "where":             "1=1",
        "outFields":         "*",
        "returnGeometry":    "false",
        "resultRecordCount": 1,
        "f":                 "json",
    },
    headers=HEADERS,
    timeout=20,
)
print(f"    HTTP status: {r2.status_code}")

if r2.status_code == 200:
    data = r2.json()
    features = data.get("features", [])
    if features:
        attrs = features[0]["attributes"]
        print(f"\n    Sample record attributes:")
        print(json.dumps(attrs, indent=6, default=str))
    else:
        print("    No features returned even for 1=1 query.")
        print("    Raw response:", data)
else:
    print(f"    ERROR: {r2.text[:300]}")

# ── Step 3: Try searching for KOWIS with no zip filter ────────────────────────
print("\n[3] Searching for KOWIS street (no zip filter)...")
r3 = requests.get(
    BASE + "/query",
    params={
        "where":             "site_str_name LIKE '%KOWIS%'",
        "outFields":         "site_str_name,site_zip,owner_name_1,total_appraised_val",
        "returnGeometry":    "false",
        "resultRecordCount": 5,
        "f":                 "json",
    },
    headers=HEADERS,
    timeout=20,
)
print(f"    HTTP status: {r3.status_code}")
if r3.status_code == 200:
    data3 = r3.json()
    feats = data3.get("features", [])
    print(f"    Records found: {len(feats)}")
    for feat in feats:
        a = feat["attributes"]
        print(f"      {a.get('site_str_name','')} | zip={a.get('site_zip','')} | owner={a.get('owner_name_1','')} | val={a.get('total_appraised_val','')}")
    if not feats:
        print("    Raw response:", json.dumps(data3, indent=4)[:400])
else:
    print(f"    ERROR: {r3.text[:300]}")

print("\n" + "=" * 56 + "\n")
