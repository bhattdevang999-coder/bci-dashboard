# Pass 3 — Multi-value attributes · Runtime Trace

> Agency R0 sprint, items 11(b) and 15. Material and Closure Type need to round-trip multiple values from template through state through writer.
>
> Fixture: same Tahari R0 ingest as Passes 1-2.
> Engine: `nis_engine/preupload_importer.py` parses, `app.py` writes.

---

## Source-of-truth shape, today vs v2

| Field | v1 template (today) | v2 template (Pass 6) | Bundle field_keys |
|---|---|---|---|
| Material | One cell, percentage string: `"80% Polyester, 15% Cotton, 5% Spandex"` | Three cells: Material 1, Material 2, Material 3 | `material#1.value`, `material#2.value`, `material#3.value` |
| Fabric Type | Same cell as Material (the percentage string) | Same percentage string in `Fabric Content Percentage` column | `fabric_type#1.value` |
| Closure | One cell. Operators today encode multi by separating: `"Zipper, Snap"`, `"Zip/Hook & Eye"` | Two cells: Closure Type, Closure Type 2 | `closure#1.type#1.value`, `closure#1.type#2.value` |

Pass 3 wires both v1 and v2 sources. v1 stays primary today; v2 columns are no-op on Tahari ingests but unblock the future template.

---

## What changed

### Importer (`nis_engine/preupload_importer.py`)

- **`_HEADER_ALIASES`** gains `material_2`, `material_3`, `closure_2`. No-op on v1 templates (the columns aren't there); future-proof for v2.
- **`_split_closures(closure_raw, closure_2_raw="")`** — new helper. Splits the v1 single-cell encoding on comma, slash, semicolon, plus, and the word " and ". Crucially **does not split on `&`** — Amazon's closure dropdown contains values like `Hook & Eye`, `Hook & Loop`, `Hook & Bar` that would lose meaning if split. The two-source signature lets v2 ingest plug in cleanly: if both raw inputs are present, both contribute to the ordered list, with the primary cell winning slot 1.
- **`style_to_form_state`** — material parsing now:
  1. Parse `s["fabric"]` into a name list and a composition string (unchanged Pass 1 behavior).
  2. If `s["material_2"]` or `s["material_3"]` are present (v2 ingest), append them ahead of any duplicates.
  3. Write `material#1/2/3.value` from the first three slots of the merged list.
- **`style_to_form_state`** — closure now writes `closure#1.type#1.value` and (when present) `closure#1.type#2.value` from `_split_closures` output.

### Writer (`app.py:5910` and `app.py:6347`)

- After writing `closure#1.type#1.value` (unchanged), check `style.get("closure_type_2")` first, then fall back to `style.state["closure#1.type#2.value"]`. Write `closure#1.type#2.value` only when a value is present — no blank writes that would clobber existing template content.
- Both write-paths (single-PT and multi-PT bulk write) handle multi-closure identically.

Material multi-write was already correct from Pass 1 — `_split_fabric_into_materials` had been writing `material#1..5` from the parsed names. No writer change needed for material.

---

## Honest limitations carried forward

1. **UI exposure is Pass 4.** The engine view today renders one input per `evaluation.fields[col]`. That means `material#2.value` and `material#3.value` already get rendered when populated — but as three separate fields, not a chip-style multi-input. The agency asked for a true "Add another" chip UI in their feedback. That naturally lives with the scope-picker UI being built in Pass 4 (same editing surface, same component). Pass 3 lands the data path; Pass 4 lands the UX polish.
2. **`& Eye` parsing depends on title-cased input.** `_split_closures` keeps `Hook & Eye` intact because the split regex doesn't include `&`. If an operator types `hook&eye` (no spaces, no caps), it still survives, but case-normalization is the writer's job downstream — we don't touch it here.
3. **The closure secondary lookup on the writer side checks two locations** (`style["closure_type_2"]` then `style["state"]["closure#1.type#2.value"]`) because both shapes exist in the codebase depending on whether the upload pipeline went through `style_to_form_state` (state shape) or the older content-driven flow (flat style dict shape). Both paths now work; neither was previously wiring multi-closure at all.
4. **Volcom multi-material rows** (e.g., `"95% Nylon, 5% Spandex"`) on the v1 template will now produce `material#1=Nylon`, `material#2=Spandex` instead of just `material#1=Nylon`. Some downstream code that assumed `material#2` was always blank may need a small look. Spot-checked: validators in `validate_field_value()` are PT-aware and accept any value from the COAT dropdown's material list for `material#2.value`.

---

## QA assertions added

- **`test_pass3_item11_material_multi`** — three-material fabric cell → all three slots populated; v2 explicit columns win over fabric-string parse; slash separator works.
- **`test_pass3_item15_closure_multi`** — comma-encoded multi → type#1 + type#2; v2 explicit column wins; `Hook & Eye` survives the split; single value leaves type#2 empty.

Both run end-to-end through `style_to_form_state` so the assertions guard the importer wiring, not just the helper functions.

Current state after Pass 3: **16 pass · 0 fail · 6 pending** (Pass 4 strategic, Pass 5 cosmetic, Pass 6 template v2).
