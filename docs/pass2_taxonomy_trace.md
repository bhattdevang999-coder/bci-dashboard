# Pass 2 — Taxonomy Unsticking · Runtime Trace

> Agency R0 sprint, items 1, 2, 3, 10, 14. Five taxonomy-stickiness bugs surfaced by Sheik's team on the Tahari Outerwear ingest (May 14, 2026). This document traces each one to a single state-management gap before any code is changed.
>
> Fixture: `data/fixtures/tahari_r0_preupload_template_2026_05_14.xlsx` (23 styles, all COAT PT, half flagged UNKNOWN until manual override).
> Engine: `nis_engine/preupload_importer.py` → `style_to_form_state()` → `evaluate_form()`.
> Frontend: `templates/index.html` — `taxonomyState`, `wsStylePTChanged()`, `taxonomyRenderForStyle()`.

---

## Where the state actually lives

Three independent stores hold pieces of "what PT is this style":

| Store | Owns | Updated by | Read by |
|---|---|---|---|
| `state.styles[i]._resolved_pt` | upload-time PT inference | `/upload` response | `taxonomyInit()` seeding |
| `wsState.styleProductTypes[sn]` | operator's manual PT pick | `wsStylePTChanged()` (PT dropdown in upload-summary table) | `/generate` payload, status badges |
| `taxonomyState.styleMeta[sn].product_type` | per-style taxonomy panel | `taxonomyInit()` only (one-shot at upload) | `taxonomyRenderForStyle()`, ITK datalist |

Nothing writes back from store 2 into store 3. That single gap is the parent of items 1, 2, 3, and 14.

Item 10 is a separate backend default-leak, not a state gap.

---

## Item 1 — ITK dropdown shows only Swimwear options regardless of Cat/Subcat

**Symptom.** Operator uploads Tahari outerwear. Per-style taxonomy panel shows the Item Type Keyword field with a free-text input. The browser's datalist suggestions are `rash-guards / swim-trunks / board-shorts / bikini-tops / …` — all swimwear.

**Trace.**

1. Tahari row's sub_class doesn't match `resolve_product_type()`'s sub_class table → PT resolves to `UNKNOWN` (line `app.py:323`).
2. `_resolved_pt = "UNKNOWN"` rides through to the frontend.
3. `taxonomyInit()` seeds `taxonomyState.styleMeta[sn].product_type = "UNKNOWN"`.
4. `taxonomyRenderForStyle(sn)` does `u = universe["UNKNOWN"] = undefined` → `cats = []`, `validItks = []`.
5. With no cascade, the renderer falls to the free-text `<input list="tax-itk-suggest">` branch (line 24829).
6. `taxonomyEnsureITKDatalist()` (line 24864) populates that datalist exactly once, with a **hardcoded swimwear list** that was probably correct on the day the M3 swimwear pipeline shipped and never revisited:

   ```js
   ['rash-guards','swim-trunks','board-shorts','bikini-tops','bikini-bottoms',
    'one-piece-swimsuits','tankini-swimsuits','bikini-sets','rash-guard-sets',
    'swim-sets','swim-shorts','fashion-swimwear-cover-ups']
   ```

That's the bug. The datalist is global (`<datalist id="tax-itk-suggest">` appended to `document.body`), built once on first style render, and never rebuilt when PT changes.

**Fix.** Replace the hardcoded list with a PT-aware builder that pulls every distinct `item_type_keyword` from `taxonomyState.universe[currentPT].item_type_keywords_by_cat_sub`. Rebuild per-style by giving each row its own `list=` id, or clear+repopulate the global datalist on each render.

---

## Item 2 — Manual PT pick from UNKNOWN doesn't repopulate Cat/Subcat/ITK

**Symptom.** Operator sees PT = UNKNOWN, opens the dropdown, picks `Jackets & Coats`. The PT cell updates. Nothing else does. The Category / Subcategory / Item Type fields on the same row stay empty selects.

**Trace.**

1. PT dropdown `onchange` calls `wsStylePTChanged(sn, newPT)` at line 20221.
2. That handler does three things — write `wsState.styleProductTypes[sn] = newPT`, update the status badge, recompute the generate-button label.
3. It does **not** touch `taxonomyState.styleMeta[sn]`.
4. The taxonomy panel for `sn` is already rendered (it lives in the expanded brief row). Even if the operator collapses and re-expands, no code path re-runs `taxonomyRenderForStyle()` after a PT change.
5. The bucket key the panel uses (`{pt}|{sub_class}|{gender_bucket}`) is now stale — it still points at the `UNKNOWN|...|Womens` bucket from upload time, not the new `COAT|...|Womens` one.

So the cascade is reading `universe["UNKNOWN"]`, which has no cats/subs, and nothing in the UI knows to re-read.

**Fix.** In `wsStylePTChanged`:
- Update `taxonomyState.styleMeta[sn].product_type` and rebuild `key`.
- Recompute `taxonomyState.styleMeta[sn].confirmed` against the new key's override.
- Call `taxonomyRenderForStyle(sn)` so the panel rebuilds from `universe[newPT]`.

Auto-derivation of Cat/Subcat for the new bucket is best-effort — if the override store has no entry under the new key, the panel just shows empty selects with valid options, which is the desired state.

---

## Item 3 — Bulk Taxonomy view: same stickiness

**Symptom.** Same as Item 2 but at the bulk modal (`taxonomyOpenBulkModal()`). Bucket row shows PT = UNKNOWN, operator picks COAT in the row's PT cell, Cat/Subcat/Item Type cells don't react.

**Trace.** The bulk modal table (line 25011) doesn't even render a PT selector per row — PT is shown as static text in the bucket cell (line 25062). The modal assumes PT is already fixed at upload time. So at the bulk view there is **no recovery path at all** for UNKNOWN PT — the operator must go back to the per-style table to set it.

**Fix.** Two options:
- **Option A (cheaper):** Add a small "Change PT" button next to the bucket label in the bulk modal that links back to the per-style row.
- **Option B (proper):** Add an inline PT select on bulk rows where `bucket.product_type === "UNKNOWN"`, wire it to a `taxonomyBulkOnPTChange(i, newPT)` that rebuilds the row from `universe[newPT]`.

Going with Option B; the agency feedback explicitly says they need to fix this without leaving the bulk view.

---

## Item 10 — Item Length Description carries DRESSES default into COAT

**Symptom.** Tahari coat in engine view shows `item_length_description = "Knee-Length"`. Operator says: "no fix button" — the field is read-only.

**Trace.**

1. `preupload_importer.py:399` writes the field from the template only if `length` column was provided. Tahari template's "Length" column is blank for coats. So writer writes `""`.
2. `evaluate_form()` runs with `apply_apparel_defaults=True`. Somewhere in that path, `_derive_item_length()` (`app.py:4692`) is called.
3. The function returns:
   - `""` for SWIMWEAR
   - `"Long"` / `"Short"` / `"Mid-Calf"` for MAXI/MINI/MIDI in style name
   - **`"Knee-Length"` for everything else** — including COAT.
4. `"Knee-Length"` is a valid value for DRESS and SKIRT. It is **not** a valid value for COAT. `dropdown_cache/COAT.json` says valid values are `Extra Short Length, Short Length, Standard Length, Long Length, Extra Long Length`.
5. The engine view renders the field. The value `"Knee-Length"` doesn't match any COAT dropdown option, so the select either falls to blank or shows the raw string. Looks like a stale leak.

The "no fix button" half: the field is rendered as a select. If the value isn't in the option list, the select doesn't show it; if the renderer additionally locks fields when the source-of-truth disagrees with the dropdown, the operator can't pick anything. Need to confirm the lock condition at render time, but the value being invalid is enough to explain the symptom.

**Fix.** PT-aware default in `_derive_item_length`:

| PT family | Default when style name carries no MAXI/MIDI/MINI/length hint |
|---|---|
| SWIMWEAR | `""` (already correct) |
| DRESS, SKIRT | `"Knee-Length"` (already correct) |
| COAT, BLAZER, SHIRT, SHORTS, PANTS, SWEATSHIRT, SNOW_PANT, SNOWSUIT | `"Standard Length"` |
| anything else | `""` (don't guess) |

The COAT dropdown values use "Length" suffix (`Standard Length`), the DRESS values use bare adjectives (`Knee-Length`). The function needs to emit the right vocabulary per PT.

---

## Item 14 — UNKNOWN taxonomy is locked from some views

**Symptom.** Agency says they can't edit taxonomy for UNKNOWN-PT rows in the per-style view.

**Trace.** There is no explicit `disabled` attribute tied to PT === UNKNOWN anywhere in the template — I searched. So the perceived lock is implicit:

- `taxonomyRenderForStyle()` reads `universe[meta.product_type]`.
- For `meta.product_type === "UNKNOWN"`, `universe["UNKNOWN"]` is undefined.
- All four selects render with just the `— pick —` option.
- The Save button at the bottom is enabled — but saving an empty quadruple fails validation at `/api/taxonomy/save` (returns `"Validation failed"`).
- To the operator this looks like a locked form.

**Fix.** When `meta.product_type === "UNKNOWN"` (or unset), don't render the cascade at all. Render a single helper block:

> Set Product Type first. The Item Type taxonomy comes from Amazon's PT-specific list — without a PT, none of these dropdowns have valid values. Use the Product Type select above to assign one, then this panel will populate.

Wire the Save button to be disabled in that mode. That converts a confusing-and-broken state into an honest one.

---

## What's testable from the QA harness

Five Pass 2 items, only two assertable cleanly from Python:

| Item | Layer | Assertable in `qa_agency_r0.py`? |
|---|---|---|
| 1 | Frontend JS | Indirect — grep template for `rash-guards` hardcoded list, expect zero matches inside `taxonomyEnsureITKDatalist`. |
| 2 | Frontend JS | Indirect — grep for `taxonomyState.styleMeta[styleNum]` inside `wsStylePTChanged`. |
| 3 | Frontend JS | Indirect — grep for `taxonomyBulkOnPTChange` definition. |
| 10 | Backend `_derive_item_length` | Yes — call function with PT=COAT, no length hint, expect "Standard Length". |
| 14 | Frontend JS | Indirect — grep for the UNKNOWN-PT helper string in `taxonomyRenderForStyle`. |

Indirect tests are honest but ugly: they verify the fix landed, not that it works end-to-end. End-to-end taxonomy-stickiness verification requires a Playwright fixture, which Pass 2 is not building.

---

## Order of operations

1. Backend fix to `_derive_item_length` + QA assertion (item 10).
2. Frontend fix to `taxonomyEnsureITKDatalist` + QA grep assertion (item 1).
3. Frontend fix to `wsStylePTChanged` + QA grep assertion (items 2 & 3 share the underlying state-sync change).
4. Frontend fix: UNKNOWN-PT helper in `taxonomyRenderForStyle` + QA grep assertion (item 14).
5. Bulk modal inline PT recovery (item 3 proper fix) + manual smoke test.
6. Commit + push.

Limitations carried forward:
- The bulk-modal PT-recovery is a UX add and won't have a real test until Pass 4 (which touches the same row-rendering paths). Pass 2 lands the function; Pass 4 wires the scope picker beside it.
- The COAT `_derive_item_length` default of "Standard Length" is the most common Amazon expectation but is not the operator's call — they should still review every coat. The fix removes the wrong-value leak, not the requirement to confirm.
