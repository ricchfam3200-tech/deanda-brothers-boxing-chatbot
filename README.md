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
