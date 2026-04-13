# BCI Dashboard v2 — NIS Creation + Catalog Health

Bhatt Commerce Intelligence — Amazon listing creation and catalog management tool.

## Features

### NIS Creation
- Upload product data (Excel/CSV) → generates Amazon-ready NIS content
- Auto-detects brand, product type, colors, sizes, fabric
- Generates titles, 5 bullet points, descriptions, backend keywords
- "Why" explanations per field — honest about what logic was used
- Inline editing with automatic feedback capture
- Regenerate any field for alternatives
- Output as CSV (always) or .xlsm (when Amazon template uploaded)
- Dynamic brand config — saved per brand, learns from feedback

### Catalog Health
- Upload catalog export + optional sales data
- Auto-detects columns from any Vendor Central or Seller Central format
- Structural checks: orphans, missing variations, duplicates, broken parent-child links
- Content completeness scoring (0-100 per ASIN)
- Revenue cross-reference — identifies which catalog issues cost money
- Variation matrix view (color × size grid)
- Fix file export — download CSV with only broken ASINs and fix instructions

## Setup

```bash
pip3 install flask flask-cors openpyxl pandas
python3 app.py
```

Open http://localhost:5000

## File Structure

```
app.py                          # Flask backend — all API endpoints + NIS engine
templates/index.html            # Frontend — single page app
uploads/templates/              # Amazon .xlsm NIS templates
uploads/products/               # Uploaded product data files
uploads/keywords/               # Uploaded Helium 10 keyword reports
uploads/output/                 # Generated NIS output files
brand_configs/                  # Saved brand configurations (JSON)
feedback/content_feedback.jsonl # Operator feedback history
```

## API Endpoints

### NIS Creation
- `POST /api/brand-config` — load brand defaults
- `POST /api/save-brand-config` — save brand config
- `GET /api/load-brand-config` — load saved config
- `POST /api/upload-template` — upload Amazon .xlsm template
- `POST /api/upload-product-data` — upload product data file
- `POST /api/upload-keywords` — upload Helium 10 CSV
- `POST /api/upload-analytics` — upload Brand Analytics CSV
- `POST /api/generate-content` — generate titles, bullets, descriptions
- `POST /api/regenerate-field` — regenerate single field
- `POST /api/submit-feedback` — store operator feedback
- `POST /api/generate-nis` — run .xlsm surgery, generate files
- `POST /api/generate-csv` — export as CSV
- `GET /api/generate-progress` — poll generation progress
- `GET /api/download/<filename>` — download individual .xlsm
- `GET /api/download-all` — download all as ZIP

### Catalog Health
- `POST /api/catalog/upload-catalog` — upload catalog file
- `POST /api/catalog/upload-sales` — upload sales data
- `GET /api/catalog/results` — get analysis results
- `GET /api/catalog/progress` — poll analysis progress
- `GET /api/catalog/fix-file` — download fix file CSV
- `GET /api/catalog/export` — download full analysis CSV
