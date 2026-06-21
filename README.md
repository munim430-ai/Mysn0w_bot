# Bangladesh RMG Factory Intelligence Pipeline

Modular data pipeline for Bangladesh Ready-Made Garment (RMG) factory intelligence. Scrapes factory data from RSC, brand supplier lists, and BGMEA; merges and deduplicates records; scores factories by equipment sales priority; exports to CRM-ready CSVs.

## Architecture

```
Module 1: RSC Scraper          --> data/raw/rsc_factories.csv
Module 2: CAP Downloader       --> data/raw/caps/{id}.pdf
Module 3: CAP Parser           --> data/raw/parsed_findings.csv
Module 4: Brand Scraper        --> data/raw/brand_suppliers.csv
Module 5: BGMEA Scraper        --> data/raw/bgmea_factories.csv
Module 6: Merger               --> data/processed/master_database.csv
Module 7: Scorer               --> data/processed/scored_database.csv
Module 8: App Exporter         --> data/processed/app_import/*.csv
```

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure GitHub PAT

```bash
cp .env.example .env
# Edit .env and add your GitHub Personal Access Token:
# GH_PAT=ghp_your_token_here
```

Generate a PAT at: https://github.com/settings/tokens (requires `repo` scope)

### 3. Run the Pipeline

```bash
# Run all modules
python src/run.py

# Run specific module
python src/run.py --module 1

# Run modules 1-3
python src/run.py --module 1-3

# Dry run (no GitHub uploads)
python src/run.py --dry-run
```

## Configuration

Edit `config.yaml` to customize:

- **GitHub**: repo owner/name, branch name, commit message template
- **Paths**: data and log directories
- **Scraping**: request delay, retries, timeout, user agent
- **Brands**: add/remove brand URLs
- **RSC**: base URLs for factory list and CAP downloads
- **BGMEA**: base URL and member directory path

## Module Details

### Module 1: RSC Factory List Scraper
Scrapes `rsc-bd.org/factories/` for factory metadata including remediation status, CAP progress, and safety training status. Falls back to Playwright for JS-rendered content.

### Module 2: CAP Bulk Downloader
Downloads Corrective Action Plan PDFs from the Accord portal. Supports resume (skips already downloaded files), exponential backoff retries, and handles 404/403/rate-limit responses.

### Module 3: CAP PDF Parser
Extracts structured findings from CAP PDFs using `pdfplumber` (table extraction) with regex fallback for inline text. Explicitly flags when boiler findings are missing from CAPs (common per CPD reports).

### Module 4: Brand Supplier Scraper
Scrapes public supplier lists from 9 major brands (H&M, Inditex, C&A, Uniqlo, Patagonia, Adidas, Nike, Walmart, Gap). Handles HTML tables, CSV downloads, JSON-LD structured data, and interactive maps via Playwright.

### Module 5: BGMEA Directory Scraper
Scrapes `bgmea.com.bd` member directory. Includes anti-scraping detection (Cloudflare, CAPTCHA) with Playwright fallback and manual CSV import template.

### Module 6: Data Merger & Deduplicator
Fuzzy-matches records across all sources using `rapidfuzz`. RSC data serves as the base; brand data adds foreign buyer relationships; BGMEA data adds contact details. Unmatched records are saved for manual review.

### Module 7: Priority Scorer
Scores each factory 0-100 based on:
- Safety findings (P1=40pts, P2=20pts)
- RSC remediation status (Terminated=50, Ineligible=40, Behind=20)
- Boiler findings (present=30pts, missing from CAP=15pts)
- Boiler age (>15yr=30pts, 8-15yr=15pts)
- Buyer pressure (EU brands=20pts, US brands=10pts)
- User notes (expanding=25pts)

Color coding: Red (80-100), Yellow (50-79), Green (0-49)

### Module 8: App Exporter
Exports to 5 CRM-ready CSV files:
- `factories.csv` - main factory table with scores
- `findings.csv` - all safety findings
- `boilers.csv` - template for boiler inventory (user-filled)
- `interactions.csv` - template for sales interactions
- `competitors.csv` - template for competitive tracking

## User Annotations

Add manual intelligence via `data/raw/user_annotations.csv`:

```csv
factory_id,factory_name,expanding,boiler_age_years,boiler_purchase_year,notes
example-id-123,Example Factory,yes,18,2007,Expanding production line
```

A template is auto-generated on first run. These annotations feed into the priority scorer.

## GitHub Integration

All data files are automatically committed to the `factory-data` branch of `munim430-ai/Mysn0w_bot`. The pipeline:
- Creates the branch if it doesn't exist
- Commits after every module
- Logs commit SHAs
- Handles rate limits and auth failures

## Data Outputs

| File | Description |
|------|-------------|
| `data/raw/rsc_factories.csv` | RSC factory list with metadata |
| `data/raw/caps/*.pdf` | Downloaded CAP PDFs |
| `data/raw/caps/manifest.csv` | Download tracking log |
| `data/raw/parsed_findings.csv` | Extracted safety findings |
| `data/raw/brand_suppliers.csv` | Brand supplier lists |
| `data/raw/bgmea_factories.csv` | BGMEA member directory |
| `data/processed/master_database.csv` | Merged master database |
| `data/processed/scored_database.csv` | Scored priority database |
| `data/processed/app_import/*.csv` | CRM-ready exports |

## Resumability

All modules support resumable execution:
- **CAP Downloader**: Skips already-downloaded PDFs
- **CAP Parser**: Processes all PDFs in directory
- **All scrapers**: Can be re-run to update existing data
- **Merger/Scorer**: Re-process from latest raw data

## Logging

Logs are written to `logs/pipeline_YYYYMMDD_HHMMSS.log` with:
- Module execution times
- Success/failure counts
- Error details
- GitHub commit SHAs

## Requirements

- Python 3.9+
- GitHub Personal Access Token with `repo` scope
- Chromium browser (installed via Playwright)

## License

MIT
