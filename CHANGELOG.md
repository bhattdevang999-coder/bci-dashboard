# Changelog

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
