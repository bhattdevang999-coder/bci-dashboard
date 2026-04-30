# Changelog

## 2026-04-30 — Brand Setup + Smart Suppressions (v0.7.3)

Three big unlocks: the operator is now prompted to set up brand-level constants on first upload, pre-upload's Cost Price + Ship Date are wired to NIS, and 19 battery false-alarms plus 6 license/government/hazmat flags are suppressed for brands that don't sell those goods. Dropping a Sage file now gives you 49 required_ok / 0 hard blockers with 30 irrelevant fields auto-silenced.

### Backend
- `nis_engine/brand_setup.py` (new)
  - `BRAND_SETUP_SCHEMA`: 10 keys the dashboard can prompt for — 4 required (vendor_code, brand_name, default_coo, department), 6 optional (care, size_system + 4 yes/no toggles).
  - `SUPPRESSION_RULES`: 4 rules that hide entire categories of false-positive verdicts when the brand's config says those categories don't apply (battery fields when `products_contain_batteries=No`, license fields when `sells_licensed_sports=No`, government fields when `is_government_contractor=No`, hazmat fields when `requires_hazmat_disclosure=No`).
  - `needs_setup()` / `load_brand_config()` / `save_brand_config()` — one-line API for the dashboard.
- `nis_engine/preupload_importer.py`
  - Header aliases extended: `shipdate`, `costprice`, `msrp` etc. now recognized.
  - `style_to_form_state()` now wires Cost Price (col Q → `cost_price#1.value`) and Due Date (col P → `rtip_earliest_shipping_date#1.value` + `merchant_release_date#1.value` + `item_booking_date#1.value`).
- `nis_engine/nis_rule_engine.evaluate_form()` now layers state in strict precedence: universal defaults ← brand defaults (from brand_configs/*.json) ← brand packaging memory ← operator-supplied form_state. Post-evaluation, suppressed fields are flipped from `required_missing` to `suppressed` with a reason tag — NEVER applied to `REQUIRED` fields (safety net).
- Response now returns `suppressed_count`, `brand_applied`, plus the existing `defaults_applied` and `packaging_applied`.

### API endpoints
- `GET  /api/rule-engine/brand-config?brand=...` — return current config + setup status + schema.
- `POST /api/rule-engine/brand-config` — save config (merges with existing, git-commits).
- `POST /api/rule-engine/import-preupload` — response now includes `brand_setup` status so frontend can prompt on first upload.

### Frontend
- NIS Rules page: import flow now checks `brand_setup.needs_setup`. If true, a setup modal appears before the per-style table with exactly the fields from the backend schema — prompts marked with a red ☆ for required, defaults pre-filled, dropdowns where the schema specifies options.
- After save, the file is re-evaluated immediately so the operator sees the impact.
- For known brands, a `✓ Brand config loaded — edit settings` hint appears, letting the operator re-open the modal to adjust (e.g. add batteries if a new line includes electronics).

### Numbers
Sage Pre-Upload R1 baseline vs. each feature:
| Config state                        | required_ok | conditional missing | suppressed |
|-------------------------------------|-----------:|-------------------:|----------:|
| Raw (no defaults)                   | 37         | 73                  | 0         |
| + apparel defaults (v0.7.2)         | 38         | 72                  | 0         |
| + packaging memory (v0.7.2, Puffer) | 46         | 64                  | 0         |
| + v0.7.3 cost/ship wiring           | 49         | 42                  | 0         |
| + v0.7.3 suppressions               | 49         | 31                  | 30        |

Net: **same file, 9 styles, 0 hard blockers, 30 fields auto-silenced**.

### Files changed
- New: `nis_engine/brand_setup.py` (254 lines)
- Modified: `nis_engine/nis_rule_engine.py` (+35 lines), `nis_engine/preupload_importer.py` (+20 lines), `app.py` (+60 lines, 2 new endpoints), `templates/index.html` (+100 lines, brand setup modal), `brand_configs/Sage_Collective.json` (upgraded with suppression flags)


## 2026-04-30 — Zero-touch Sage Pre-Upload (v0.7.2)

Any brand's pre-upload .xlsx now flows to **zero hard blockers** through the dashboard without operator field-by-field input. The operator drops the file, the engine reports per-style readiness, and the only per-brand setup is package dimensions once per sub-class (remembered forever).

### Backend
- `nis_engine/apparel_defaults.json` — 14 universal defaults that every non-electronic apparel listing inherits (batteries=No, dangerous_goods=Not Applicable, age_range=Adult, size_system=US, variation_theme=Size/Color, etc.). Operator values always win — defaults only fill in blanks.
- `nis_engine/brand_packaging_memory.json` — operator-confirmed package dimensions per (brand, product_type, sub_class). Saved once, reused forever, git-committed after each save to survive Render redeploys.
- `nis_engine/preupload_importer.py` — schema-tolerant parser for TLG-style pre-upload .xlsx files. Matches columns by name (lowercased, punctuation-stripped) so R0 vs R1 column shuffles don't break it. Auto-detects brand from TLGDIV NAME. Translates pre-upload rows into NIS form-state dicts using sub-class-to-taxonomy map.
- `evaluate_form()` now accepts `apply_apparel_defaults=True`, `brand`, `sub_class` params. Merges defaults + packaging memory + operator state, then evaluates.

### API endpoints
- `POST /api/rule-engine/import-preupload` — multipart upload. Returns per-style readiness with hard-missing list.
- `GET  /api/rule-engine/packaging` — read memory (all or by brand+PT+sub_class).
- `POST /api/rule-engine/packaging` — save dims for a (brand, product_type, sub_class); git-commits.
- `POST /api/rule-engine/evaluate` — now accepts `brand`, `sub_class`, `apply_apparel_defaults` for full packaging+defaults-aware evaluation.

### Frontend
- NIS Rules page: new **↱ Import Pre-Upload** button in the header. Uploading any pre-upload .xlsx renders:
  - Top header: file name, auto-detected brand, count of styles, ready/needs-attention pills, % ready meter.
  - **Package dimensions editor** grouped by sub-class. Enter LxWxH + weight once per sub-class. Saved permanently, re-applied to every style on save. Button re-evaluates the whole file instantly.
  - Per-style table: style ID, sub-class, UPCs, colors/sizes, OK/hard/conditional counts, status badge (✓ Ready or N blockers).
  - Hard-missing fields listed inline when a style isn't ready.

### Verified on Sage Pre-Upload R1
- 9 styles, 80 UPCs → **9 ready / 0 hard blockers** with zero operator input beyond picking the file.
- After saving Puffer package dimensions once (4 numbers + units): Puffer styles climb from 38 → 46 required-OK. The same dims are used for all 6 future Puffer styles without re-entry.
- Brand auto-detected: Sage Collective. Categories auto-mapped: Puffer → Women's Outerwear → Down and Parkas, Faux Wool Outerwear → Wool, etc.

### Files added/modified
- `nis_engine/apparel_defaults.json` (new, 22 lines)
- `nis_engine/brand_packaging_memory.json` (new, seed empty)
- `nis_engine/preupload_importer.py` (new, 295 lines)
- `nis_engine/nis_rule_engine.py` (+110 lines: defaults/packaging loaders, evaluate_form params)
- `app.py` (+110 lines: 3 new API endpoints, evaluate signature updated)
- `templates/index.html` (+200 lines: Import button + results panel + packaging editor)


## 2026-04-30 — Engine-backed Taxonomy Cascade (v0.7.1)

Fix for the "empty Item Type dropdowns" issue on COAT (and any product type whose `taxonomy_universe.json` was hand-curated and incomplete). Pulls cascade values directly from the universal NIS rule-engine bundles so every Amazon-defined Cat → Sub → Item Type Keyword path is selectable.

### Backend
- `nis_engine/taxonomy_builder.py` — walks every product-type bundle, parses Amazon's encoded named-range paths (`COATproduct_category.value.<CAT>.product_subcategory.value.<SUB>.item_type_keyword1.value`), maps them back to human labels using the top-level category list, and emits an `item_type_keywords_by_cat_sub` map per product type.
- `_load_taxonomy_universe()` now auto-enriches from the engine when the static file is missing cascade data. Static + engine universes are merged, engine wins on cascade ITKs, static wins on hand-curated ITN names.
- `_validate_taxonomy_quadruple()` now soft-warns (doesn't block save) when an operator picks an `item_type_keyword` outside Amazon's published cascade list.
- `taxonomy_universe.json` rebuilt with full cascade data: COAT now has 129 cascade ITKs (was 0), DRESS 86, SWIMWEAR 92, BLAZER 12.

### Frontend
- Bulk Taxonomy Confirmation modal: Item Type column now renders a real cascade dropdown when Amazon defines one for the chosen Category + Subcategory. Falls back to flat ITN list, then to a free-text input with a clear amber message ("No item type defined for this combo — free-text input is allowed") when neither exists.
- Subcategory change triggers Item Type re-render via new `taxonomyBulkOnSubChange()`.
- Modal banner now shows memory hint: "✓ N of M buckets were auto-matched from your previous uploads. Confirmed entries are saved permanently — next time you upload styles in the same sub-class, you won't see this prompt."
- Action button label updated to "Confirm & Save" with a "Saved for future uploads" caption.

### Memory persistence (already existed; now clearly surfaced)
- Per-bucket overrides keyed by `(product_type, sub_class, gender_bucket)` in `taxonomy_overrides.json` — git-committed in a thread after every save so they survive Render redeploys.
- Sub-class → product-type map in `subclass_product_type_map.json` (auto-learned + hand-curated fallback).

### QA
- Live verified end-to-end: pick `Women's Outerwear` → `Wool` on a fresh Sage Faux Wool bucket and the Item Type dropdown correctly shows `wool-outerwear-coats`. Combos without Amazon-defined ITKs surface the friendly fallback message instead of an empty dropdown.


## 2026-04-30 — NIS Rule Engine (v0.7.0)

Universal Amazon NIS conditional-logic engine. Reads any `.xlsm` template, parses every CF + DV formula, evaluates against form state at runtime. Same engine works for all 31 templates; if Amazon ships a new one, operator clicks "Rebuild from templates" and it auto-adapts.

### Backend
- New package `nis_engine/`:
  - `nis_formula_parser.py` — tokenizer + recursive-descent Excel formula parser. Handles `AND/OR/NOT/IF/COUNTIF/VLOOKUP/ISERROR/LEN/INDIRECT`, comparisons, `&` string concat, `'Sheet'!$A$1:$B$16000` sheet-qualified ranges, and `$A$1` absolute refs. AST is JSON-serialisable.
  - `nis_rule_evaluator.py` — walks the AST against a form-state dict, returns Excel-accurate verdicts. Short-circuits IF/AND/OR, case-insensitive string comparison, proper `#N/A` / `#REF!` / `#VALUE!` propagation, ISERROR catches both.
  - `nis_rule_extractor.py` — reads a `.xlsm`, writes `nis_rules/{PRODUCTTYPE}.json` with fields + rules + named ranges + VLOOKUP tables. Detects the template sheet via `workbook.xml` relationship map (handles both relative and absolute target paths). Merges multiple templates for the same product type (e.g. 7 sources -> DRESS.json).
  - `nis_rule_engine.py` — high-level API the dashboard calls: `evaluate_form(product_type, form_state)` returns per-field verdicts, rule trail, dropdown source, trigger cells.
- CF rule semantics are keyed off dxfId: dxf 0/3 = valid (green), dxf 1 = filled, dxf 2 = required-missing (red). Only dxf 2 rules fire the operator alert.
- Pre-built bundles in `nis_rules/`: 16 product types covering 31 source templates, ~10K rules total, 0 needing review. Index at `nis_rules/__index__.json`.
- New endpoints on `app.py`:
  - `GET  /api/rule-engine/index` — product types + coverage
  - `GET  /api/rule-engine/bundle/<PT>` — field definitions + coverage
  - `POST /api/rule-engine/evaluate` — run rules against form state
  - `POST /api/rule-engine/override` — log an operator override to `feedback/overrides_log.jsonl`
  - `GET  /api/rule-engine/overrides` — browse logged overrides
  - `POST /api/rule-engine/rebuild` — re-extract bundles from all uploaded templates

### Frontend
- New sidebar nav "NIS Rules" page at `#engine-page`:
  - Product-type picker with live coverage header (fields / rules / needs-review / dropdowns / source count).
  - Listing state entry grid, grouped by section (Listing Identity / Variations / Product Identity / ...).
  - "Load Sage sample" button pre-fills Sage Collective's COAT listing state (vendor code QT5G8, parent, womens, China).
  - "Evaluate" triggers backend evaluation; verdict pills + three-bucket review (Required-Missing expanded / Needs Review / Required-OK collapsed / Optional collapsed).
  - Per-field card shows: field label + column, current value, trigger cells ("Depends on: D7, A7"), the CF rule source (first 60 chars), and an "Override & flag" button for red-verdict fields.
  - "Rebuild from templates" button for the manual re-extract workflow.

### Brand configs + taxonomy
- `brand_configs/Sage_Collective.json` — Sage Collective brand profile (vendor code QT5G8, default COO China, Female gender, puffer / wool outerwear default styles, COAT primary product type).
- `subclass_product_type_map.json` — sub-class → Amazon product type map covering Sage's 9 styles (Puffer / Faux Wool Outerwear / Wool Outerwear / 3/4 Coat → COAT) plus common blazer/swim/dress mappings.

### Testing
- 38 parser unit tests + 55 evaluator tests + 30 end-to-end engine tests — all pass.
- Cross-template smoke test: 26,371 real formulas parsed across all 31 templates with zero unknowns.
- Sage Pre-Upload spot-check: engine correctly flags 95 missing fields (Bullet Point, Style, Apparel Size System, etc.) for a parent listing with basic identity filled.

### Files changed
- `app.py` (+200 LoC engine init + 5 endpoints)
- `templates/index.html` (+300 LoC: page + script + CSS)
- `nis_engine/*.py` (4 new modules, ~1500 LoC)
- `nis_engine/test_*.py` (3 test files)
- `nis_rules/*.json` (16 bundle files + __index__.json, ~35MB)
- `brand_configs/Sage_Collective.json`
- `subclass_product_type_map.json`


## 2026-04-23 — Historical Bulksheet Snapshots (v0.6.0)

Every bulksheet upload is now auto-saved as a dated snapshot. With ≥2 snapshots the Ad Readiness card renders a full trend view: ineligible-over-time line chart, since-prior deltas, newly blocked / newly recovered tables, chronic flippers, and per-reason deltas.

### Backend (`app.py`)
- `SNAPSHOTS_DIR = ./snapshots/` with 52-file retention (~1 year weekly).
- `_save_snapshot(ad_truth_lookup, filename)` auto-writes a dated JSON on every bulksheet upload: `{id, timestamp, filename, total, ineligible, eligible, asins: {asin: {status, reasons, raw_reasons}}}`. Atomic tmp → rename.
- `_list_snapshots()` / `_load_snapshot(id)` / `_prune_snapshots()`.
- `_compute_trends(scored_rows, max_history=12)` — returns `None` with <2 snapshots; otherwise a full trends block:
  - `series` (per-snapshot ineligible/total/pct) for the line chart
  - `newly_blocked` + `newly_blocked_total` (current vs prior snapshot, enriched with title + category from catalog)
  - `newly_recovered` + `newly_recovered_total` (with `was_blocked_for` reasons)
  - `chronic_flippers` (ASINs with ≥3 state changes across the window)
  - `reason_deltas` (current vs prior ineligible count per reason)
- New endpoints:
  - `GET  /api/catalog/snapshots` — list all saved snapshots
  - `GET  /api/catalog/snapshots/<id>` — fetch a single snapshot's full ASIN map
  - `DELETE /api/catalog/snapshots/<id>` — remove a snapshot
- Bulksheet upload response now includes `snapshot_id` and `snapshot_count`; detection summary reads "Saved as snapshot #N".
- `run_catalog_analysis()` response now always includes `trends` (or null) and `snapshot_count`.

### Frontend (`templates/index.html`)
- New "History & Trends" section inside the Ad Readiness card, with a snapshot-count badge that updates on every bulksheet upload.
- Inline SVG line chart of ineligible ASINs over time: area fill + gridlines + per-point circles with hover tooltips showing exact values. No library; scales with container width.
- 6 stat tiles: Currently blocked / Since prior (↑/↓ arrow + green/red) / Newly blocked / Newly recovered / Chronic flippers / Snapshots loaded.
- Reason-deltas table with green/red delta coloring.
- Newly blocked table: ASIN, title, category, Amazon reasons.
- Newly recovered table: ASIN, title, category, previously-blocked-for reasons.
- Chronic flippers table: ASIN, title, category, flip count, current status pill.

### QA
Built 3 dated bulksheet fixtures simulating a realistic trend: week 1 (2 blocked), week 2 (6 blocked, 4 newly blocked), week 3 (5 blocked, 2 newly blocked, 3 recovered).

- Series correctly shows 2 → 6 → 5 on the line chart
- Newly blocked: B0ADOOSTCK01 (Out of stock), B0ADNOIMG001 (Missing main image) — matches the plant
- Newly recovered: B0ADLOSTBB01 (BB), B0ADLOWSTK01 (OOS), B0C01MENSM (BB) — all 3 caught
- Reason deltas: Lost Buy Box −2 (green), Missing main image +1 (red) — exactly right
- Since-prior tile: −1 with down-arrow (success green) because total dropped from 6 to 5
- Reconciliation still renders side-by-side below the trends section
- GET /api/catalog/snapshots returns 3 entries with distinct timestamps
- DELETE endpoint works and retention enforced

Regression: NIS + Phase 2 taxonomy still 100% clean (59 styles, 13 buckets, 0 errors).

### Files touched
- `app.py` — +~200 lines: snapshot storage helpers, `_compute_trends`, 3 new endpoints, auto-save on bulksheet upload, `trends` + `snapshot_count` in analysis response.
- `templates/index.html` — +~180 lines: History & Trends section, `chRenderTrends` (inline SVG chart + 4 stat-summary tables), hydration hook in `chRenderAdReadiness`.
- `.gitignore` — adds `snapshots/` (runtime data, not source).

---

## 2026-04-23 — Step C: Tier-2 Ground Truth (v0.5.0)

Adds a third upload slot for Amazon's Sponsored Products bulksheet. When present, Amazon's flag overrides the Ad Readiness proxy per ASIN, the PROXY badge flips to GROUND TRUTH, and a full predicted-vs-actual reconciliation is rendered.

### Backend (`app.py`)
- `AD_BULKSHEET_FIELD_MAP` with fuzzy aliases for Advertising Console columns (Advertised ASIN, Eligibility Status, Eligibility Reasons, Campaign Name, etc.)
- `AMAZON_REASON_CODE_MAP` maps 19 Amazon reason codes (ASIN_NOT_BUYABLE, NOT_FEATURED_OFFER, OUT_OF_STOCK, SEARCH_SUPPRESSED, AD_POLICY_VIOLATION, BOOK_FORMAT_INELIGIBLE, PRICE_NOT_COMPETITIVE, etc.) onto our canonical severity labels for clean reconciliation.
- `_normalize_ad_reason()` — exact + substring matching, tolerates Amazon's occasional wrapped-in-prose reason text.
- `_parse_ad_bulksheet(rows, headers)` — returns `{asin: {status, raw_reasons, reasons}}`. Splits multi-reason cells on `;`, `|`, `,`. Infers status from reasons when status column is missing.
- `run_catalog_analysis()` now accepts `ad_truth_lookup`. When supplied:
  - Amazon's flag overrides the proxy per matched ASIN; proxy at-risk hints preserved as additional context.
  - Each scored row gets `ad_source` (`proxy` / `actual` / `proxy_only_not_in_bulksheet`) + `ad_raw_codes` + `ad_proxy_status` + `ad_proxy_reasons` for transparency.
  - `eligibility.ground_truth` flips to `true`.
  - `eligibility.reconciliation` block added: matched / catalog_only / bulksheet_only, TP/TN/FP/FN, accuracy, precision, recall, and up to 20 mismatch examples with both sides' reasons.
- New endpoint: `POST /api/catalog/upload-ad-bulksheet` (parses bulksheet, stores lookup, re-runs analysis with ground truth).
- Existing `upload-catalog` and `upload-sales` endpoints now pick up `ad_truth_lookup` from session state if present.

### Frontend (`templates/index.html`)
- Third upload zone "Ad Eligibility Bulksheet" (Ground Truth tag) alongside Catalog + Sales, with help text explaining exactly where to download it in Advertising Console.
- `chUploadAdBulksheet()` wired into drag-and-drop and file-input handlers.
- Ad Readiness badge auto-flips **PROXY** → **GROUND TRUTH** (caption text updates too) when the response's `ground_truth` flag is true.
- New **Predicted vs Actual — proxy calibration** panel inside the Ad Readiness card. Only shown when a bulksheet is uploaded.
  - Color-coded accuracy pill (green ≥90%, amber 75-89%, red <75%).
  - 6 stat cards: Matched / True Positives / False Positives / False Negatives / Catalog-only / Bulksheet-only.
  - Mismatches table with per-ASIN side-by-side reasons (FP vs FN labels).

### QA (populated fixture)
- `TLG_Ad_Bulksheet_test.xlsx` fixture — 18 rows: 5 proxy-confirms-Amazon agreements, 9 both-eligible agreements, 1 intentional false positive (B0BADCONTENT: proxy flagged missing image, Amazon says eligible), 1 intentional false negative seeded but out-of-catalog, 2 bulksheet-only ASINs (not in catalog).
- End-to-end upload through the dashboard:
  - Badge flipped to GROUND TRUTH; caption read "Calibrated against Amazon Advertising Console bulksheet."
  - Stats: eligible 13 / at_risk 25 / ineligible 5 (down from proxy's 3/34/6 because Amazon confirmed one proxy call was wrong).
  - Reconciliation: **93.3% accuracy**, precision 83.3%, recall 100%. Matched 15, TP 5, FP 1, FN 0, catalog-only 28, bulksheet-only 3.
  - Mismatch table shows B0BADCONTENT as FP with both sides' reasons side by side.

Regression: NIS + Phase 2 taxonomy still 100% clean (59 styles, 13 buckets, 0 errors).

### Files touched
- `app.py` — +~200 lines: bulksheet field map + reason-code map, `_normalize_ad_reason`, `_parse_ad_bulksheet`, new `/api/catalog/upload-ad-bulksheet` endpoint, `ad_truth_lookup` parameter on `run_catalog_analysis`, per-row override logic, reconciliation computation, session-state plumbing through existing upload endpoints.
- `templates/index.html` — +~110 lines: third upload zone, `chUploadAdBulksheet`, reconciliation panel rendering with accuracy pill, stat cards, and mismatch table.

---

## 2026-04-23 — Ad Readiness (v0.4.0)

Extends Catalog Health with a PPC eligibility audit. Predicts which ASINs Amazon would block from Sponsored Products, groups them by root cause, and gives per-reason fix actions. Ships as a new section inside the existing Catalog Health page — no new uploads, no new pages.

### Backend (`app.py`)
- `_eligibility_for_row()` — Tier-1 proxy with 8 rules ordered by severity:
  1. Listing inactive
  2. Search-suppressed (separate column)
  3. Restricted category (adult, used/refurbished/renewed, firearms, tobacco/vape, Rx)
  4. Out of stock (quantity ≤ 0) or Low inventory (≤ 5 → at-risk)
  5. Lost Buy Box (Buy Box Winner = No)
  6. Missing main image
  7. Price > 10% above Buy Box Price
  8. Content score < 70
- Status classification: `eligible` | `at_risk` | `ineligible`. Blocking reasons (1–6 except low-inventory and price/content) flip status to ineligible; non-blocking reasons to at_risk.
- `_eligibility_fix_action()` returns operator-facing fix guidance per reason.
- `SEVERITY_WEIGHTS` extended with 9 eligibility reason codes.
- `AD_READINESS_ISSUES` set for UI filtering (`group: "ad_readiness"` vs `"hygiene"` on each issue).
- `AD_BLOCKING_ISSUES` set to distinguish blockers from at-risk reasons.
- `RESTRICTED_AD_CATEGORIES` list of Amazon-policy-ineligible categories.
- Response now includes `eligibility` block:
  ```
  {
    total, eligible, at_risk, ineligible,
    eligible_pct, at_risk_pct, ineligible_pct,
    revenue_at_risk, fast_fix_count,
    reasons: [{reason, asin_count, top_category, top_category_count, revenue_at_risk, fix_action, severity, blocking}, ...],
    categories: [{category, total, eligible, at_risk, ineligible, eligible_pct}, ...],
    ground_truth: false,
  }
  ```

### Frontend (`templates/index.html`)
- New "Ad Readiness — PPC Eligibility Audit" card between Summary and Filters sections.
- 4 stat tiles: Ad-Ready / At Risk / Blocked for Ads / Fast-Fix ASINs (+ Revenue at Risk when sales data uploaded).
- Reason breakdown table: one row per reason with color dot (red=blocking, amber=at-risk), ASIN count, top category, revenue at risk, concrete fix action.
- Eligibility by Category: horizontal segmented bars (green/amber/red) per category, width proportional to category size, with eligibility % and "N blocked" flag.
- PROXY / GROUND TRUTH badge (switches when Tier-2 Ad Bulksheet is uploaded, roadmapped for Step C).
- New "View" filter in the issues table filter row: `All issues | Catalog hygiene only | Ad Readiness only`.
- New Issue Type options: Lost Buy Box, Out of Stock, Suppressed, Inactive, Price above BB, Content weak, Restricted category, Low inventory.

### QA
Extended test catalog (`TLG_Catalog_test_data.xlsx`) with 7 additional rows, one per eligibility failure mode:
- `B0ADLOSTBB01` Buy Box Winner = No → flagged Lost Buy Box ✓
- `B0ADOOSTCK01` quantity 0 → flagged Out of Stock ✓
- `B0ADSUPPRES1` Search Suppressed = Yes → flagged Listing Suppressed ✓
- `B0ADINACT001` Status = Inactive → flagged Listing Inactive ✓
- `B0ADPRICE001` List $79.95 vs BB $59.95 (+33%) → flagged Price above Buy Box ✓
- `B0ADLOWSTK01` quantity = 3 → flagged Low inventory (at-risk) ✓
- `B0ADNOIMG001` blank Main Image URL → flagged Missing main image (no ads) ✓

Result: 6 blocked ASINs, 34 at-risk, 3 ad-ready. All 7 reason types appear in the breakdown table with correct severity coloring. Category bar shows 7% eligible with 6 blocked. View filter isolates 41 Ad Readiness issues vs 65 Hygiene issues.

Regression: NIS + Phase 2 taxonomy still 100% clean (59 styles, 13 buckets, 0 errors).

### Deferred to Step C
- Tier-2 ground truth: upload Amazon Advertising Console SP bulksheet with `Eligibility Status` + reason codes; cross-check against proxy.
- Amazon Business Report integration for real revenue-at-risk figures (today uses sibling-ASIN sales average when available).
- Suppressed Listings Report integration for exact suppression reason codes.

### Files touched
- `app.py` — +~150 lines: severity map extensions, eligibility constants, `_eligibility_for_row`, `_eligibility_fix_action`, `_num`, `_bool_field`, wired into `run_catalog_analysis`, new `eligibility` block in response.
- `templates/index.html` — +~130 lines: Ad Readiness section HTML, `chRenderAdReadiness`, `chEscapeHtml`, View filter, extended Issue Type options, hook in `chLoadResults`/`chApplyFilters`.

---

## 2026-04-22 — Catalog Health Step A (v0.3.0)

First production wiring of Catalog Health. The tab now runs Layer 1 (content completeness scoring) and Layer 2 (structural integrity checks) end-to-end against any Amazon catalog export or the TLG Catalog Health Template.

### Input handling
- `_find_header_row()` locates the real header row in multi-row templates (handles the TLG template's banner on row 1, category bands on row 3, headers on row 4, REQUIRED/OPTIONAL markers on row 5, sample row on row 6).
- `_looks_like_metadata_row()` skips REQUIRED/OPTIONAL marker rows and `B0XXXXXXXXX` sample rows.
- `_pick_catalog_sheet()` / `_pick_sales_sheet()` pick the right sheet by name from a multi-sheet workbook (Catalog Snapshot / Monthly Performance).
- Expanded `CATALOG_FIELD_MAP` to 50+ field aliases covering the full TLG template columns plus Vendor Central and Seller Central conventions.
- Expanded `SALES_FIELD_MAP` to capture Monthly Performance's period, traffic, conversion, and advertising columns.

### Layer 1 — Content completeness (0-100 per ASIN)
Title 80-200 chars (15 pts), 5 bullets each ≥50 chars (15 pts, 3 per bullet), description ≥200 chars (10 pts), backend keywords ≤250 bytes (10 pts), main image (10 pts), 6+ additional images (10 pts), price > 0 (10 pts), brand (5 pts), color + size (5 pts), category (10 pts). Colors: green 90+, yellow 70-89, orange 50-69, red <50.

### Layer 2 — Structural checks
- **Orphan detection** — child ASINs whose parent isn't in the dataset
- **Variation matrix gaps** — per-parent color×size grid; flags every missing cell with a specific `Add variant: Color=X, Size=Y` fix
- **Duplicate children** — same color+size twice under one parent
- **Wrong parent link** — child brand mismatches parent brand
- **Single-child parents** — likely data-entry mistakes
- **Broken variation theme** — empty color cell under a COLOR/SIZE parent

### Dashboard UX
- "Catalog Health" section in the sidebar with an "Upload & Analyze" nav item
- Two upload zones: Catalog File (required) + Sales Data (optional)
- Detection summary card with mapped/missing pills (`✓ asin → Child ASIN`)
- 6 stat cards: Total ASINs / Parents / Children / Avg Health Score / Critical Issues / Total Issues
- Priority-sorted issues table with severity badges (Critical / High / Medium / Low), per-row fix actions, and Export Fix File / Full Analysis buttons
- Variation Matrix viewer — pick a parent, see Color×Size grid with ✓ (healthy) / ⚠ (incomplete) / ✗ (missing)

### QA
Built a populated Volcom test catalog with intentional issues in each category:
- Orphan (`B0ORPHAN01` pointing at non-existent parent) → 1 Critical flag
- Variation gaps (Pink sizes S/L/XL missing, Navy M missing, White sizes) → 7 High flags with exact color/size fixes
- Duplicates (2 ASINs with Navy/S under same parent) → 2 Medium flags, each referencing the other
- Brand mismatch (Roxy child under Volcom parent) → 1 Medium flag
- Single-child parent → 1 Low flag
- Bad content (short title, no bullets, no image) → content score 0, flagged across 3 categories
- Broken variation theme (empty color under COLOR/SIZE parent) → caught by matrix

Result: 66 issues detected across 36 test ASINs with zero false negatives on the injected failure modes. Fix-file CSV + Full Analysis CSV both export cleanly.

Regression: NIS upload/generation + Phase 2 taxonomy UX still 100% clean (59 styles, 13 buckets, 0 errors).

### Files touched
- `app.py` — expanded CATALOG_FIELD_MAP/SALES_FIELD_MAP, added `_find_header_row`, `_looks_like_metadata_row`, `_pick_catalog_sheet`, `_pick_sales_sheet`, extended `read_file_to_rows` with `sheet_kind` parameter.
- Templates already in place from prior work; this release makes them actually parse correctly.

---

## 2026-04-22 — Phase 2 Taxonomy UX + NIS Row-Spacing Fix

### Phase 2 — Taxonomy UX on Dashboard

Delivers the operator-facing surface on top of the Phase 1 backend (commit `d3059d8`). The dashboard can now drive per-item-type taxonomy confirmation without leaving the page.

**Added to `templates/index.html`:**
- **Taxonomy banner** after upload summarising confirmed vs unconfirmed buckets, with `[Review & Confirm]` and `[Generate Anyway]` CTAs.
- **4 cascading dropdowns** inside each expanded style row (Category → Subcategory → Item Type Keyword → Item Type Name). Subcategory list is silently filtered by the selected category using `universe.subcategories_by_category`. If the previously-selected subcategory is no longer valid after the category change, it is auto-cleared.
- **`Save as default for …`** button per style — persists the triple `(product_type, sub_class, gender_bucket)` override to the backend and shows a toast.
- **Bulk-confirm modal** (`[Review & Confirm]`) — one row per unconfirmed bucket with the same 4 dropdowns + individual `Confirm` and `Save All`.
- **Soft-block on Generate** when any bucket is unconfirmed. Dialog: "Some item-type buckets have no confirmed taxonomy. Auto-derived values will be used. Click Cancel to review and confirm them first, or OK to generate with auto-derived taxonomy." `[Generate Anyway]` sets `window.__taxonomyAck` to bypass on subsequent clicks.
- **Source indicator per style** (`✓ confirmed` green pill / `⚠ auto-derived` amber pill) on the expanded taxonomy panel.
- Hydration wiring: `taxonomyInit(data)` on upload response, `taxonomyRenderForStyle(sn)` on first row expand.

**QA performed (this commit):**
- Upload Volcom swim (59 styles) → 13 buckets detected, banner renders.
- Save override on `SWIMWEAR|Rashguard|Mens` → `taxonomy_overrides.json` persists entry, banner updates to 1 confirmed / 12 unconfirmed.
- Bulk-confirm modal opens showing only the 12 remaining unconfirmed buckets.
- Soft-block fires on Generate Content. `[Generate Anyway]` sets ack flag and lets generation proceed.
- Cascade recalibration: picking `Men's Swimwear > Trunks`, then switching category to `Boys Private Label`, auto-clears the now-invalid subcategory and repopulates the dropdown with `[Baby Boys PL Swimwear, Boys PL Swimwear]`.
- End-to-end NIS generation: 2/2 styles succeed, 0 errors, files land in `uploads/output/`.

### NIS Row-Spacing Fix

User-reported: "the NIS file generation has issues with row spacing, they're on top of each other."

**Root cause.** Amazon's .xlsm templates ship with `customHeight=True, height=12.75` on every data row (a single text-line's worth of height) and `wrap_text=None` on bullet/description columns. Our writers were copying cell styles from row 7 into each data row but never:
1. Setting `wrap_text=True` on long-text cells (bullets up to 500 chars, titles 82-91 chars, descriptions 200+ chars).
2. Releasing the fixed row height on the data rows.

Result: when Excel opened the file, it crammed 7-line wrapped bullets into 12.75pt rows, which rendered as text visually stacking on top of subsequent rows.

**Fix** (added to `app.py` above `do_xlsm_surgery`):
- `LONG_TEXT_FIELD_IDS` — set of field IDs that must wrap (5 bullets, description, item_name, style, model_name, generic_keyword, item_type_keyword).
- `_is_long_text_field(field_id)` helper.
- `_apply_long_text_alignment(cell, cached_alignment)` — sets `wrap_text=True, vertical=top, shrink_to_fit=False` while preserving horizontal/indent from the template's row-7 style.
- `_clear_row_heights_for_auto_fit(ws, start_row=7)` — sets `row_dimensions[r].height = None` for rows 7+ so Excel auto-sizes based on wrapped content. Header rows 1-6 keep their heights (row 2 stays 42, row 3 stays 28).

Both writers — `do_xlsm_surgery()` (per-style) and `_generate_category_file()` (combined per product-type) — now call the clear helper after clearing data values and apply long-text alignment when writing any field in `LONG_TEXT_FIELD_IDS`.

**Validation.**
- Generated NIS_Volcom_436008622.xlsm (Rashguard parent + 4 child variants).
- Row-dimensions inspection: rows 1-6 keep heights (12.75 / 42 / 28 / 12.75 / 12.75 / 12.75); rows 7-59 have `height=None`, so Excel auto-fits.
- Bullet cells: `wrap_text=True, vertical=top` in every data row.
- Converted to PDF via LibreOffice and visually inspected page 24 (bullet_point column): each 149-char bullet renders as 7 neatly wrapped lines inside an auto-sized row with clear separator borders between variants. No stacking.

### Files touched
- `templates/index.html` — +~570 lines (Phase 2 UX module, banner, per-row taxonomy panel, bulk modal, soft-block wiring).
- `app.py` — +~50 lines (`LONG_TEXT_FIELD_IDS`, three helpers, two writer call-sites).
- `taxonomy_overrides.json` — updated with test override for `SWIMWEAR|Rashguard|Mens`.

---

## 2026-04-22 — Volcom Swimwear QA Pass

End-to-end QA of the Volcom pre-upload file (59 styles, 726 variants) through upload → routing → generation → NIS .xlsm download. Every finding below is triaged Blocker / Major / Minor based on whether Amazon would reject the file or a partner would notice on a demo.

### Regression snapshot

| Check bundle | Result |
|---|---|
| 9 original blockers (all regenerated + audited across 10 sampled styles) | 10/10 styles pass all per-style checks |
| Expanded regression (blockers + product_subcategory filled) | **75/75 pass** |
| Product subcategory filled across all 59 Volcom swim styles | **59/59 filled** (was 0/59) |
| Bullet 1 diversity across 21 rashguards | **19/21 distinct** (was 1/21) |
| Bullet 2 diversity across 21 rashguards | **20/21 distinct** (was 1/21) |
| Description diversity across 21 rashguards | **17/21 distinct** (was 1/21) |
| Rashguards with bogus `Knee-Length` | **0/21** (was 21/21) |
| Bullet 2 style-name stuffing | **0/59** (was 59/59) |

### Commits shipping these fixes

- `f8150ef` — 9 blockers (gender in title, parent row, variation theme, vendor code, youth T sizes, target_gender, age range, size_class, swim-set mapping)
- `cea148f` — Bullet 1/2/description variation + backend keyword stem-based dedup (#11, #12, #16)
- `2f2d61a` — product_subcategory populated for all 59 styles + item_length blank for SWIMWEAR (#13, #15)

---

### Blockers — all resolved

| # | Issue | Status | Fixed in |
|---|---|---|---|
| B1 | All 59 titles said "Volcom Female …" (brand config `gender: "Female"` leaking into every title regardless of division) | ✅ | `f8150ef` — `generate_title()` now takes `style_gender=` param that trumps `brand_cfg["gender"]`. Added `_gender_title_word()` that returns `Men's` / `Women's` / `Boys'` / `Girls'` / `Kids'` based on style-derived gender + style_name. Cleared `gender: "Female"` from Volcom.json. |
| B2 | .xlsm had zero Parent rows — only Children. Amazon ingest rejects orphan children | ✅ | `f8150ef` — Both writers (`do_xlsm_surgery`, `_generate_category_file`) now emit 1 Parent row before children, using `write_shared` and setting `parentage_level = Parent`. |
| B3 | `variation_theme = "COLOR"` written to every row (Amazon expects multi-axis) | ✅ | `f8150ef` — Dynamically computed from variant set: `SIZE/COLOR` when both vary (Amazon-valid), `COLOR` or `SIZE` when only one varies. `COLOR/SIZE` (which was hardcoded before) isn't in the Amazon dropdown. |
| B4 | `rtip_vendor_code#1.value` blank in every row | ✅ | `f8150ef` — `write_shared_row` now falls back to `brand_cfg["vendor_code_full"]` when session is empty. Volcom.json gained `"vendor_code_full": "Volcom, us_apparel, 7E8G6"`; fuzzy matcher auto-corrects to Amazon's canonical `"Volcom Apparel, us_apparel, 7E8G6"`. |
| B5 | Youth sizes stripped of "T" (2T → 2 — size data corruption) | ✅ | `f8150ef` — New `_derive_youth_size_info()` maps `2T` → `2 Years` (Amazon-valid), sets `size_class = Age`. Plain numeric kid sizes 4/5/6/7/8 also map to `N Years`. |
| B6 | 26 youth styles set `target_gender = Unisex` on "Little Boys" styles | ✅ | `f8150ef` — `_derive_gender_department` now uses `style_name` as secondary signal ("Little Boys" → Male/boys). Writer refines further: `"boys"` in style_name forces `target_gender = Male`. |
| B7 | `age_range_description = Adult` on toddlers | ✅ | `f8150ef` — Youth-aware via `_derive_youth_size_info`: Toddler / Little Kid / Big Kid. Also populates `special_size_type` (Toddler Boys / Little Boys / Big Boys). |
| B8 | `size_class = Alpha` when sizes are 2T/3T/numeric | ✅ | `f8150ef` — Returned from `_derive_youth_size_info`: `Age` for numeric/T sizes, `Alpha` for S/M/L/XL. |
| B9 | `item_type_name = "Bikini Set"` on boys' 2-piece swim sets | ✅ | `f8150ef` — `_derive_item_type_name()` now gender+style_name aware. Boys' swim shirt + trunk combos return `Rash Guard Set`; generic boys' sets return `Two Piece Swimsuit`; only girls/women get `Bikini Set`. Same routing for `item_type_keyword` (`rash-guard-sets` / `swim-sets` / `bikini-sets`). |

### Majors — resolved

| # | Issue | Status | Fixed in |
|---|---|---|---|
| M10 | Title casing broke acronyms: `UPF 50+` → `Upf 50+` | ✅ | `f8150ef` — `_title_case_preserve_acronyms()` restores UPF/UV/USA/US/NFL/MLB/NBA/LS/SS. Also fixed `Men'S` → `Men's` (possessive broken by `.title()`). |
| M11 | Identical bullet 1 + description copy across 21 rash guards | ✅ | `cea148f` — Added `SWIM_UPF_B1_OPENERS` (6 × 5 tails = 30 combos), `SWIM_LIFESTYLE_B1_OPENERS` (5×5 = 25), `SWIM_DESCRIPTION_OPENERS` (8 openers × 4 closers = 32 combos). Indexed by two independent hashes of `style_num` so two styles with the same name still get different rotations. |
| M12 | Backend keyword stem-stuffing (`rash guard shirt rash guard shirt rash guards rashguard`) | ✅ | `cea148f` — Stem-aware dedup in `generate_backend_keywords`: drops any phrase whose singular-stemmed tokens are all already covered by an earlier phrase. Singular/plural (`-s`, `-es`) collapsed for comparison. |
| M13 | `product_subcategory` blank for all 59 styles | ✅ | `2f2d61a` — New `_derive_swim_product_subcategory()` with gender + subclass + style_name routing. Values sourced directly from the Swimwear.xlsm's 114 `defined_names` (Men's Swimwear: Board Shorts/Trunks/Briefs/Misc; Women's Swimwear: 9 options including Bikini Top Separates, One-Piece Swimsuits, Rashguards, Two-Piece Swimsuit Sets; Swim/youth: Boys Swim Bottoms/Rashguards/Sets, Girls Two Piece Swim, etc.). Wired through all 4 call sites. |
| M14 | Youth `product_category = "Men's Swimwear"` (should be `Swim`) | ✅ | `f8150ef` — `_derive_amazon_product_category` now takes `style_name` + `department` params; youth signals ("boys", "girls", "toddler") force `Swim` regardless of derived Male/Female. |
| M15 | `item_length_description = Knee-Length` on rash guards | ✅ | `2f2d61a` — `_derive_item_length()` returns blank for any SWIMWEAR style or swim subclass. The field isn't on the Swimwear template anyway — the preview was lying. |
| M16 | Bullet 2 stuffed full style name verbatim ("This Long Sleeve Hooded Rashguard – Loose Fit Sun Shirt Upf 50+ Protection features…") | ✅ | `cea148f` — Bullet 2 now uses `{itn_lower}` (the short item-type name). Added `SWIM_B2_OPENERS` (5) × `SWIM_B2_TEMPLATES` (5) = 25 combinations, rotated by independent style_num hashes. |

### Majors — open

| # | Issue | Status | Notes |
|---|---|---|---|
| M17 | LLM vision flow (Claude image + brief → field generation) not tested end-to-end with this dataset | 🟡 | Requires uploading a product image through the dashboard expand-row UI. Code path is wired (`/api/upload-style-image` → base64 to Claude); just untested on a real image payload. |

### Minors — open (not blocking partner demo)

| # | Issue | Status | Notes |
|---|---|---|---|
| m1 | COO default blank in Volcom brand config; 6/59 styles flagged "missing COO" as action items | 🟡 | Working as designed (flag + leave blank per "accuracy not negotiable" rule) — needs per-brand decision. |
| m2 | SKU format inconsistency between UI preview (bare style_num) and xlsm (7E8G6-prefixed) | 🟡 | Cosmetic — both are valid; could align for clarity. |
| m3 | Package dimensions and weight empty for every style | 🟡 | Correctly flagged in UI as "use Apply All" — working as designed. Needs operator input. |

---

### Methodology

End-to-end QA re-run after every fix batch:

1. Restart local server (port 5000).
2. POST `Copy-of-Pre-Upload-Template-Amazon-Swim.xlsx` with `brand=Volcom` → `/api/upload-product-data`.
3. POST `/api/generate-content` in `rules` mode, poll `/api/content-progress` until `done`.
4. Pull 10 styles across all divisions (Mens x3, Womens x3, Toddler x2, Big Boys x2) + full 59-style sweep for aggregate diversity checks.
5. Download actual `.xlsm` via `/api/download-style/<sn>` for each sample.
6. Open with `openpyxl` and verify each field_id → cell value against Amazon-expected output.

Verification categories:
- Structural: Parent row present, child count = variant count, variation theme valid per Amazon dropdown
- Identity: Vendor code filled per brand config, SKU format consistent
- Gender/age: target_gender Male/Female (never Unisex at row level), age_range matches style_name (Toddler/Little Kid/Big Kid/Adult), special_size_type populated for youth
- Content: no duplicate bullet 1/2/description across same-subclass styles, no style-name stuffing in bullet 2, backend keywords deduped
- Taxonomy: product_category correct per division (Men's/Women's/Swim), product_subcategory filled with Amazon-valid dropdown value, item_type_name correct per gender+subclass (no "Bikini Set" on boys)
- Sizes: youth T-sizes preserved (as "N Years"), size_class = Age/Numeric/Alpha per actual size format
