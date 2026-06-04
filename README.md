# Houston Wholesale Lead Engine

Pulls live property data from Harris County HCAD, filters for individual motivated sellers, scores every lead, and outputs a ranked CSV ready for skip-tracing and outreach.

## Target Area
Northeast Houston — zip codes: 77016, 77020, 77021, 77022, 77026, 77028, 77029, 77050, 77078, 77093

## Setup (one time only)

1. Install Python from https://www.python.org/downloads/  
   — Check **"Add Python to PATH"** during install
2. Double-click **setup.bat** to install required libraries

## Running

**Option 1 — Double-click RUN.bat** (easiest)  
Runs all 20 target streets and saves `final_leads.csv`

**Option 2 — Command line**
```
# By street list:
python houston_leads.py --streets sample_input.csv

# By zip code:
python houston_leads.py --zip 77028

# All 10 target zip codes:
python houston_leads.py --zip all

# With LGBS tax delinquent file (download from taxsales.lgbs.com):
python houston_leads.py --streets sample_input.csv --lgbs-file lgbs_harris.xlsx
```

## Output: final_leads.csv

| Column | Description |
|--------|-------------|
| rank | Lead rank (1 = best) |
| final_score | Score 0–10 |
| final_signals | Why this property is flagged |
| owner_name | Property owner |
| mailing_address | Where to send mail |
| property_address | Property location |
| appraised_value | HCAD appraised value |
| transfer_date | When ownership last changed |
| mail_name | Formatted name for mail merge |

## Lead Scoring

| Signal | Points |
|--------|--------|
| Single-family (A1) | +3 |
| Out-of-state mailing address | +2 |
| Probate / Estate of | +2 |
| Tax delinquent (LGBS) | +3 |
| Transfer date 2020 or later | +1 |
| Value under $150K | +1 |
| Value under $80K | +2 |
| No building / vacant | +1 |

## Important Notes

- Must run from a **home/residential internet connection**
- Harris County GIS blocks cloud/datacenter IPs
- Data is live from HCAD — refresh monthly for best results
- LGBS tax sale list: download manually from taxsales.lgbs.com → Harris County

---

## PropStream North Houston Search

`propstream_search.py` pulls leads directly from PropStream for the north side of Houston: **Studewood, 5th Ward, Northline, and Aldine**.

### Target Zones & Zip Codes

| Zone | Zip Codes |
|------|-----------|
| Studewood | 77008, 77018 |
| 5th Ward | 77020, 77026 |
| Northline | 77022, 77093 |
| Aldine | 77032, 77037, 77038, 77039, 77060, 77073 |

### Setup Your Credentials

```bash
# Windows
set PROPSTREAM_EMAIL=your@email.com
set PROPSTREAM_PASSWORD=yourpassword

# Mac / Linux
export PROPSTREAM_EMAIL=your@email.com
export PROPSTREAM_PASSWORD=yourpassword
```

### Running PropStream Search

```bash
# All three setups, all north Houston zones (recommended):
python propstream_search.py

# Single setup only:
python propstream_search.py --setup 1   # Tired Landlord
python propstream_search.py --setup 2   # Vacant Land
python propstream_search.py --setup 3   # Pre-Foreclosure / Liens

# Specific zone only:
python propstream_search.py --zone Aldine
python propstream_search.py --zone "5th Ward"

# More results per zone (default: 50):
python propstream_search.py --limit 100

# Custom output file:
python propstream_search.py --out my_north_houston_leads.csv
```

### The Three Search Setups

**Setup 1 — Tired Landlord** (SFR single-family)
- Owner Type: Individual (no LLCs or corporations)
- Ownership Duration: 10+ years
- Occupancy: Absentee owner (rental or vacant)
- Estimated Equity: 50–100%
- *Sorted by years owned — longest hold first*

**Setup 2 — Vacant Land**
- Property Type: Vacant / Unimproved Land
- Occupancy: Vacant
- Ownership Duration: 5+ years
- Estimated Equity: 90–100% (free and clear)
- *Sorted by equity percent — clearest title first*

**Setup 3 — Pre-Foreclosure / Liens** (highest motivation)
- Pre-foreclosure filings, OR
- Lien amount ≥ $2,000 (real financial pressure)
- *Sorted by lien amount — most distressed first*

### PropStream Output Columns

| Column | Description |
|--------|-------------|
| rank | Lead rank across all zones/setups |
| setup | Which search setup produced this lead |
| zone | North Houston zone (Studewood, 5th Ward, etc.) |
| owner_name | Property owner |
| mailing_address | Where to send direct mail |
| property_address | Property location |
| property_type | Property type (SFR, Land, etc.) |
| years_owned | How long current owner has held |
| occupancy_status | Absentee / Vacant / Owner-occupied |
| estimated_value | PropStream AVM estimate |
| equity_percent | Estimated equity % |
| equity_amount | Estimated equity dollar amount |
| lien_amount | Total lien amount (if any) |
| pre_foreclosure | Yes/No pre-foreclosure flag |
| tax_delinquent | Yes/No tax delinquent flag |
| acct_num | Property account / parcel number |
