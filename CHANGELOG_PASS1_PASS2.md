# NIS Dashboard — Pass 1 & Pass 2 Changelog

**Scope:** PT-aware refactor (Pass 1) + 17 individually-QA'd field-level fixes from Sage R1 team feedback (Pass 2). Every patch was code-edited then verified before moving to the next.

**Status:** READY FOR DEPLOY — pending operator approval.

---

## Cross-PT validation (gate before deploy)

| Test | Result |
|---|---|
| Sage R1 COAT (89 variants, 9 styles) | All 17 flagged fields write correctly. 89/89 sleeve length = "Long Sleeve". |
| Volcom Swimwear (Rashguards, real pre-upload) | PT routes to SWIMWEAR, fabric splits to material#1/#2, "Hooded" snaps to "Hooded Neck", SKUs split correctly, parent/child structure clean. |
| Synthetic DRESS (re-tagged Sage rows) | Routes to DRESS, writes apparel_silhouette=Mini, item_type_name=Dress, sleeve defaults to Short Sleeve per PT. |
| Synthetic SHIRT / PANTS / SHORTS | Each routes to correct PT, writes 9 rows clean, no exceptions. |
| Mixed-PT Health card (6 PTs, 9 styles) | All 6 PTs report ready=true, session_label = "Coats (4) + Dresses (1) + Shorts (1) + Swimwear (1) + Shirts (1) + Pants (1)". |
| App import + 94 routes | Clean import, all 8 critical endpoints registered. |

---

## Pass 1 — PT-aware foundation

| ID | Change | Files |
|---|---|---|
| P1-1 | New `nis_engine/pt_defaults.json` (16 PTs × 13 keys: sleeve/closure/neck defaults, dimensions strategy, DG default, title noun, label_singular/plural, template_file). | new |
| P1-2 | New `nis_engine/pt_defaults.py` — `get_pt_default()`, `pt_label()`, `pt_writes()`, `template_label_for_session()`. | new |
| P1-3 | `_derive_sleeve_length()` made PT-aware (COAT → "Long Sleeve" instead of universal "Sleeveless"). | app.py |
| P1-4 | `derive_sleeve_type()` made PT-aware (was hardcoded Sleeveless even when caller passed COAT). | app.py |
| P1-5 | `normalize_color()` PT-aware fuzzy snap to dropdown_cache/{PT}.json (Cream→Off White, Truffle→Brown, Camel→Brown for COAT, Wine→Red, Cognac→Brown, Charcoal→Grey). | app.py |
| P1-6 | DG Regulation now reads `_pt_defaults.pt_writes(pt, "dg")` + `_pt_defaults.get_pt_default(pt, "default_dg_regulation")`. | app.py (×2 writers) |
| P1-7 | Dynamic UI labels: template badge, hero descriptors, "X coats / Y dresses" pluralization in dashboard cards. | templates/index.html |
| P1-8 | `/api/template-coverage` endpoint — PT × {template, rules, dropdowns} health snapshot. | app.py |
| P1-9 | Health card UI in dashboard (one row per PT). | templates/index.html |
| P1-10 | Per-style trace chip showing resolved PT + reason. | templates/index.html |

**Pass 1 QA findings caught:**
- `derive_sleeve_type()` itself had hardcoded Sleeveless default (not just `_derive_sleeve_length()`). Both fixed.
- 89/89 COAT rows verified showing "Long Sleeve" after the fix.

---

## Pass 2 — Field-level fixes (17 patches, A through Q)

Each patch was applied to **both writers in parallel**: `do_xlsm_surgery()` (single-style download path) and `_generate_category_file()` (per-PT batch). Every patch was QA'd against Sage R1 in isolation before the next was started.

| ID | Symptom | Root cause | Fix |
|---|---|---|---|
| P2-A | Prices written as `"66"` text instead of `66.00` numeric — Amazon validator rejects | `cell.number_format` was being **overwritten** by cached row-7 styles after our value write | `cell.number_format = "0.00"` applied **AFTER** cached style restoration; PRICE_FIELDS set defined in both `write_cell` + `wc` helpers |
| P2-B | Item title truncated mid-word ("…with Faux Fur Co") | Naive byte-count truncation | Drop comma-separated segments first; word-boundary fallback dropping trailing connectors (with/and/etc); never break mid-word |
| P2-C | `vendor_sku` (parent-level) wasn't deriving cleanly from variant SKUs | No helper existed | New `_derive_parent_sku_from_variants()` extracts `F26-{styleNum}`; new `_derive_child_sku()` uses source SKU verbatim |
| P2-D | `cost_price` from pre-upload not surfacing on parent row | Lookup missing in `_generate_category_file` | Added cost_price lookup at line 5494; parent row writes both list_price + cost_price with try/except float coercion |
| P2-E | All styles writing same closure | `closure_type` only read from `content` (LLM), not `style.get()` | Closure flow now reads `_style.get("closure_type")` first. Sage R1: 18/18 styles correct (Button/Zipper/Belted) |
| P2-F | "Number of Pockets" never written | Pre-upload `pockets` column not surfaced in style dict | Importer parses pockets → `style.get("pockets")` → `number_of_pockets#1.value`. 89/89 rows |
| P2-G | Item Type Keyword (ITK) — operator can't pick valid value if cascade misses it | Pure text input with no fallback | `<select>` with cascade-filtered options + custom-value fallback when ITK not in cascade. `taxonomyOnChange` rebuilds on cat/sub change |
| P2-H | Page jumps to top after every field save | No scroll preservation | Snapshot scrollY + row offset before re-render; restore after. Located in `wsSaveField` |
| P2-I | Item Type Name (ITN) field shows but PT has empty universe → user types invalid | No conditional render | ITN hidden for PTs with empty `item_type_names` universe; helper note shown |
| P2-J | "Womens" / "Mens" inconsistent case across PTs | `_derive_gender_department` was lowercasing | Titlecase "Womens"/"Mens" baseline; fuzzy matcher snaps to PT's exact case |
| P2-K | `material#1` writing the full fabric string, not split | No splitter | New `_split_fabric_into_materials("80% Polyester, 15% Cotton")` → `["Polyester","Cotton"]`. Wires material#1/2/3; fabric_type gets full string. `style.get("fabric")` fallback added at line 5525 |
| P2-L/M | Some PTs use `collar_style#1.value`, others use `neck#1.neck_style#1.value` | Single hardcoded path | New `_neck_field(col_map)` returns the right one. Sleeve flow already had `style.get` fallback in P2-K |
| P2-N | `special_feature`, `lifestyle`, `body_type`, `height_type` blank for COAT | No derivers | New `_derive_special_features()` (keyword→Hooded/Belted/etc), `_derive_lifestyle()` (→Casual/Business Casual/Formal). body_type/height_type defaults to "Regular" (valid in COAT dropdown — was incorrectly attempting "All Body Types") |
| P2-O | No way to test single-PT upload without parent row | No toggle | `session_data["skip_parent_row"]` toggle + UI checkbox in Health card. When True: no parent row, no parentage_level, no child_parent_sku_relationship, no variation_theme |
| P2-P | Operator typing free text where dropdown universe exists | UI didn't know about dropdowns | `wsState.fieldDropdowns` populated from new `/api/dropdowns-for-session`. `wsEditField` uses `<input list=>` + `<datalist>` for fields with valid values; falls back to plain input |
| P2-Q | `coat_silhouette_type` field always blank | No mapper | New `_derive_coat_silhouette()`: Puffer→Quilted, Trench→Trench Coat, etc. Only fires for COAT. `type_of_jacket` parsed in app.py line 2346 |

---

## New files

- `nis_engine/pt_defaults.json` — 16 PTs × 13 default keys
- `nis_engine/pt_defaults.py` — loader API
- `CHANGELOG_PASS1_PASS2.md` — this file

## Modified files

- `app.py` (~9,930 lines): two parallel writers (`do_xlsm_surgery`, `_generate_category_file`); `normalize_color`; `_derive_sleeve_length`; `derive_sleeve_type`; `_derive_gender_department`; new endpoints; new helpers
- `templates/index.html` (~10,420 lines): Health card, ITK select, scroll-preserve, fit-type datalist, ITN conditional render
- `nis_engine/preupload_importer.py`: `pockets`, `type_of_jacket` added; titlecase Womens/Mens

## New endpoints

| Path | Purpose |
|---|---|
| `GET /api/template-coverage` | Health card data: PT × template/rules/dropdowns wiring + per-style trace |
| `GET /api/dropdowns-for-session` | Returns valid dropdown values per PT for fit_type, etc. (used by inline editor datalists) |
| `POST /api/set-skip-parent` | Toggle skip-parent mode for single-style testing |

---

## Caveats / known limits (intentional)

1. **`Faux Wool` doesn't map to a coat silhouette.** Acceptable per "When unsure, flag and leave blank" rule. `Puffer`, `Trench`, `Anorak`, `Vest`, `Pea Coat`, `Parka` all map.
2. **`Jeans` subclass doesn't auto-resolve to PANTS** (no keyword in fuzzy heuristic). Handled by operator confirmation prompt; saved to learned map.
3. **DG Regulation field name is COAT/SWIMWEAR-specific** (`supplier_declared_dg_hz_regulation#1.value`). DRESS template doesn't expose this column — write is silently skipped via `_pt_defaults.pt_writes(pt, "dg")`.
4. **Synthetic dresses test used Sage R1 with subclass re-tagged.** Real Volcom/Sage dresses pre-upload would test the full PreUpload Style sheet → variant merge path; current synth does not.
5. **`Short` subclass routes to SWIMWEAR** (learned map from prior session). `Boardshorts` correctly routes to SHORTS. Operator can override per-style in UI if needed.
6. **`item_type_keyword` cascade for SWIMWEAR sub_subclass=Hoodie** falls back to `rash-guards` because no learned map for Hoodie sub-sub. Output is valid for Amazon but operator may want a more specific keyword.

---

## Deploy checklist

- [x] Sage R1 (COAT) — 17 patches all individually QA'd, integrated test clean
- [x] Volcom Swimwear pre-upload — full pipeline runs, output structurally correct
- [x] Synthetic Dresses — DRESS PT routes correctly, no exceptions
- [x] Synthetic Shirt/Pants/Shorts — all route + write 9 rows each
- [x] Mixed-PT Health card — 6 PTs all show ready=true
- [x] App imports cleanly, 94 routes registered
- [ ] Operator final review + push to GitHub
- [ ] Render auto-deploy verified at https://tlg-amazon-intelligence-dashboard.onrender.com
