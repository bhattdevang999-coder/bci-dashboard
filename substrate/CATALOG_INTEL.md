# CATALOG_INTEL

Module spec · v0.1 · locked 2026-07-01

## Purpose

Client-facing dashboard. A brand (TLG client OR Novelle itself) uploads a
catalog export + optional sales sheet. Atlas returns:
- A coverage matrix (which analyses are runnable / partial / blocked)
- Every analysis its file supports
- Drilldown to ASIN level on any finding
- Guidance on what additional data unlocks deeper insight

Positions Atlas as an intelligence layer on top of the existing rules engine,
not a monitoring surface (that's Catalog Health) or an editor (that's Listing
Manager).

## Non-goals for v1

- No visualizations. Text + tables only. Viz layer reads the same JSON later.
- No PDF/HTML report export. Add after 2-3 clients validate the shape.
- No API-driven continuous refresh. Snapshot model only — client uploads a
  new file whenever they want a new analysis.
- No redaction / internal-only findings layer. All findings visible to
  whoever has workspace access.
- No competitor benchmarking. Analyses run on the uploaded data only.

## Canonical file schema

Template: `data/fixtures/ROXY-Atlas_Catalog_Data_Template_20260519-FINAL.xlsx`

**Workbook structure**: three sheets — README, Catalog, Sales (Optional).

**Catalog sheet — required columns**:
- ASIN, Parent ASIN, Title, Brand, Category

**Catalog sheet — recognized columns** (39 total):
ASIN, Parent ASIN, SKU, UPC, Style #, Model Name, Title, Bullet 1-5,
Description, Backend Keywords, Color, Size, Variation Theme, Parent/Child,
Main Image URL, Other Image URLs, Image Count, Video Count, Brand,
Category, Subcategory, Item Type Keyword, List Price, Sale Price, Buy Box
Price, Buy Box Winner, Quantity, Fabric/Material, Country of Origin, Care
Instructions, Item Weight, Package Dimensions, A+/EBC Status, Listing
Status, Fulfillment Method.

**Sales sheet — columns**: ASIN, Sessions, Units, Revenue, CVR,
Period Start, Period End.

## Data model

### New table: `catalog_snapshots`
One row per uploaded file. Immutable.
```
snapshot_id       uuid  PK
workspace_id      text
uploaded_at       timestamptz
uploaded_by       text
file_name         text
file_s3_path      text          # or local uploads/ path
row_count_catalog int
row_count_sales   int
period_start      date          # from sales sheet
period_end        date
notes             text
```

### New table: `asin_sales_metrics`
Time-series per-ASIN sales. Accumulated + deduped by (workspace, asin, period_end).
```
workspace_id  text
asin          text
period_start  date
period_end    date
sessions      int
units         int
revenue_numeric
cvr_pct       numeric
snapshot_id   uuid  FK → catalog_snapshots
inserted_at   timestamptz
PRIMARY KEY (workspace_id, asin, period_end)
```

### New table: `catalog_intel_findings`
Reuses the same shape as `catalog_audit_findings`. Distinct table because
findings are snapshot-scoped (not brand-current-state scoped).
```
finding_id     uuid PK
snapshot_id    uuid FK
workspace_id   text
asin           text | null       # null for catalog-wide findings
rule_name      text
severity       critical | high | medium | low | strategic
priority_score numeric
evidence_json  jsonb
proposed_fix   text
```

### Extension to `asin_metadata.ground_truth_fields` (existing JSONB column)
On catalog ingest, the following field keys are populated when present in the
sheet (merged, does not overwrite existing values):
- `a_plus_status`, `image_count`, `video_count`, `list_price`, `sale_price`,
  `buy_box_winner`, `country_of_origin`, `care_instructions`,
  `fabric_material`, `sub_category`, `variation_theme`, `listing_status`,
  `fulfillment_method`, `backend_keywords`, `quantity`.

## Coverage matrix

Every analysis declares its input columns + minimum fill rate. The matrix
computes status per analysis:
- **runnable** — all inputs present at ≥80% fill
- **partial** — inputs present at 5-80% fill (analysis runs with a warning
  banner naming the reduced sample)
- **blocked** — any required input at <5% fill (analysis does not run)

Also surfaced: a **360° opportunities** list — data types NOT in the file at
all (reviews, BSR, ad spend, returns, historical) with a one-liner on what
each would unlock.

## Analyses in v1 (all text output, all runnable on the Roxy file)

Each analysis has: `id`, `label`, `required_columns[]`, `runnable_at_fill`,
`headline_metric`, `drilldown_query`, `severity_taxonomy`.

1. `concentration_pareto` — top N% ASINs = X% of revenue
2. `dead_inventory_cohort` — sessions=0 AND units=0 in period
3. `long_tail_cohort` — sessions 1-500, low CVR
4. `active_cohort` — sessions >500 or units >10
5. `core_cohort` — top revenue contributors (auto-computed cutoff)
6. `a_plus_lift` — same-parent A+ vs non-A+ children, revenue delta
7. `image_count_dist` — histogram, flag ASINs with <5 or <7 images
8. `bullet_completeness_dist` — histogram of bullets per ASIN
9. `title_length_dist` — histogram, flag <80 or >200 chars
10. `list_price_dist` — histogram, band segmentation, outliers
11. `subcategory_rollup` — per-subcategory revenue, ASIN count, A+ %
12. `style_family_concentration` — orphan parents, orphan children,
    mega-clusters (families with >50 children)
13. `variation_theme_integrity` — orphan themes, missing themes on parents
14. `description_presence` — % filled, length distribution
15. `buy_box_ownership` — % of ASINs where uploader is buy box winner
16. `fill_rate_report` — every column, % filled, sample values

## Non-runnable analyses (blocked in the Roxy file, unlocked with more data)

Surfaced in the coverage matrix as the 360° opportunity list:
- `promo_depth` — needs Sale Price populated
- `compliance_coo` — needs Country of Origin populated
- `compliance_care` — needs Care Instructions populated
- `search_term_coverage` — needs Backend Keywords populated
- `review_pareto` — needs review_count column (not in schema yet)
- `rank_decay` — needs BSR column + historical snapshots
- `ad_efficiency` — needs ad spend + ACoS (not in schema yet)
- `return_rate_by_asin` — needs return data (not in schema yet)
- `trend_decay` — needs ≥2 historical snapshots
- `competitor_gap` — needs external benchmark data

## Ingest workflow

1. Client uploads workbook via drop-zone
2. Snapshot row created; file saved to `uploads/catalog_intel/<workspace>/<snapshot_id>/`
3. Catalog sheet parsed → `asin_metadata` upserted (merge, don't overwrite)
4. Sales sheet parsed (if present) → `asin_sales_metrics` upserted, deduped
   on (workspace, asin, period_end)
5. Coverage matrix computed
6. Rules engine runs each runnable analysis → writes to
   `catalog_intel_findings`
7. UI redirects to the coverage matrix + findings summary

Failure modes:
- Missing required columns → return error before writing anything
- Duplicate ASINs within the sheet → keep last, warn
- Sales period overlaps existing snapshot → dedupe on period_end (last upload wins)
- >100MB file → reject with size cap message

## Client-facing UI shape

Sidebar nav: **Catalog Intel** (Beta pill) in the MONITOR column.

Landing state (no snapshots yet):
- Drop-zone: "Drop your Amazon catalog export"
- Link: "Download template" (serves the ROXY template file)
- Brief copy: "We'll analyze up to 15 dimensions and tell you exactly what
  more data unlocks."

Post-upload state:
- Header: workspace, snapshot timestamp, file name
- **Coverage Matrix** (top of page): 3-column table (Analysis, Status, Reason)
- **Runnable analyses** (middle): each analysis as a card with headline
  metric + "See ASINs" link
- **360° Opportunities** (bottom): analyses blocked by missing data with
  guidance on what to add

Drilldown: clicking "See ASINs" from a finding routes to Listing Manager
with a filter for that finding's ASIN list.

## Auth model

Both TLG staff and clients can sign in.
- TLG staff: sees a workspace picker at top of every page, can switch
  between all client workspaces
- Client: locked to their own workspace_id, no picker visible
- Uses the existing `atlas_workspace_id` cookie pattern

Distinction enforced at the API layer, not UI (server checks user's
allowed workspaces on every request).

## Build sequence

v0.1 (this commit) — spec + module shell + nav entry
v0.2 — Ingest routes (catalog + sales), snapshots table, S3-style file save
v0.3 — Coverage matrix endpoint + UI
v0.4 — Rules pack (analyses 1-8 of v1 list)
v0.5 — Remaining analyses (9-16) + drilldown wiring
v0.6 — 360° opportunities panel + polish
v0.7 — Report export (PDF)

Each version is one push, reviewable, mergeable.

## Version history

- **v0.1 · 2026-07-01** — Spec locked. Names: module = Catalog Intel; DB
  tables = catalog_snapshots, asin_sales_metrics, catalog_intel_findings.
  File schema = the ROXY-Atlas_Catalog_Data_Template_20260519-FINAL.xlsx.
  Auth = both TLG staff (with workspace picker) + clients (locked to their
  workspace). Snapshot model, accumulate + dedupe on sales, keep all
  snapshots. Partial fill (5-80%) runs with warning.
