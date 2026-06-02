# Agency R0 Feedback Tracker — Tahari Outerwear, May 14 2026

> Source: Sheik's agency operator feedback after running the Tahari R0 outerwear ingest (23 styles × 9.6 average UPCs) through Atlas Bulk Upload NIS.
> Fixtures checked in at `data/fixtures/tahari_r0_*.xlsx`.
> Regression harness: `qa_agency_r0.py`.
>
> Status: **Pass 3 landed.** Passes 0 + 1 + 2 + 3 complete. Items 1-3, 5, 8-11, 13-15 fixed. QA harness: 16 pass, 0 fail, 6 pending (Passes 4-6).

---

## Pass plan (locked, top-down execution)

| Pass | Scope | QA target | Acceptance |
|---|---|---|---|
| 0 | This document + fixture + skeleton QA | — | All 15 items have a file:line hypothesis; fixture ingest reproduces them |
| 1 | Parser + Engine Verdict accuracy | Items 5, 9, 11, 13, 8 | Zero false-positive blockers; all 5 named fields flow through |
| 2 | Taxonomy unsticking | Items 1, 2, 3, 10, 14 | Brand-switch resets dropdowns; UNKNOWN PT recoverable |
| 3 | Multi-value attributes | Items 11, 15 | Material[] and Closure[] round-trip cleanly |
| 4 | Scope-aware editing UI | Strategic ask 1 | `scope=brand_always` on style 1 reads through to style 2 |
| 5 | UI polish | Items 6, 7, 12 | Hover-blackout, bullet ALLCAPS-dash, Content tab sync all fixed |
| 6 | Pre-upload template v2 | Strategic ask 2 | New template + v1→v2 mapping layer; both accepted at ingest |

---

## The 15 specific items — root-cause map

Confidence column: **H** = exact line located, **M** = strong hypothesis, **L** = needs runtime trace to confirm.

### Pass 1 cluster — parser → verdict (single source-of-truth fix)

#### Item 5 — Engine Verdict shows false "blockers"
- **Confidence:** M
- **Symptom:** Verdict reports missing Bullet Points / Vendor SKU / Fabric Type when the pre-upload template had them populated.
- **Likely cause:** `app.py:10941` reads `evaluation.get("fields", ...).verdict == "required_missing"` on the *pre-default* state. `evaluate_form` does apply apparel defaults (`apply_apparel_defaults=True` at `app.py:10937`), but the field set the verdict iterates may be a snapshot taken before defaults merge, or it reads from a different field-name namespace than `style_to_form_state` writes.
- **File:** `app.py:10937`, `app.py:10941`, `nis_engine/nis_rule_evaluator.py` (the `evaluate_form` callee).
- **Dependency:** Likely upstream of items 9, 11, 13 — if verdict reads from a stale state, those fields all appear missing.
- **Fix sketch:** Force verdict to re-read from post-defaults state. Add a `state_at_verdict_time` snapshot in the response and assert it contains the same field keys we wrote in `style_to_form_state`.

#### Item 9 — Closure Type set in template, not surfacing in engine
- **Confidence:** H
- **Symptom:** Closure Type populated in pre-upload (e.g., "Zipper" on Tahari puffers) does not appear in the engine view's structured-attribute panel.
- **What's actually being written:** `nis_engine/preupload_importer.py:287` — `"closure#1.type#1.value": s.get("closure") or ""`. The value is set.
- **Likely cause:** The engine view's renderer is reading a different field key (`closure_type#1.value` or `closure#1.value`) than the importer writes (`closure#1.type#1.value`). Name mismatch between writer and reader.
- **File:** `nis_engine/preupload_importer.py:287` (writer side), engine view template + `substrate/field_suggest.py` reader side.
- **Dependency:** Fixing this likely also fixes Item 5's false "Fabric Type missing" if the same key-mismatch pattern exists for Fabric Type.

#### Item 11 — Material and Fabric Type formats swapped
- **Confidence:** H
- **Symptom:** Material attribute shows percentages ("100% Polyester"), Fabric Type attribute shows description words. Operator expectation is the reverse: Material = description list (Polyester, Spandex), Fabric Type = composition with percentages.
- **What's actually being written:** `nis_engine/preupload_importer.py:285-286`:
  ```python
  "material#1.value":     s.get("fabric") or "",   # writes the % string
  "fabric_type#1.value":  s.get("fabric") or "",   # same value into both
  ```
  Both fields receive the same source string (Fabric Content Percentage column from template, e.g., `100% Polyester`).
- **Fix sketch:** Two-stage split. Parse `s.get("fabric")` (e.g., `100% Polyester` or `95% Polyester, 5% Spandex`) into:
  - `material#1.value` → ordered list of material names: `["Polyester"]` or `["Polyester", "Spandex"]`
  - `fabric_type#1.value` → the original percentage-bearing string
- **Dependency:** Item 15 (multi-value Material) lives here too — Pass 3 will extend this.

#### Item 13 — Jacket shows as Sleeveless; Sleeve Type editable, Sleeve Length not
- **Confidence:** H
- **Symptom:** A jacket with long sleeves comes through as Sleeveless. Operator can edit Sleeve Type but not Sleeve Length.
- **What's actually being written:** `nis_engine/preupload_importer.py:231-310` — `style_to_form_state` does **not** map `sleeve_length` from the template at all. The closest field set is `closure#1.type#1.value`. Sleeve Length is missing from the writer.
- **Fix sketch:** Add a Sleeve Length column to `_HEADER_ALIASES` and a `sleeve_length#1.value` key to `state`. Add an editable input on the engine view.
- **Dependency:** Pass 3 (multi-value) doesn't apply here; this is a missing column, not a multi-value problem.

#### Item 8 — Tool identifies Women's elsewhere, but Department/Target Gender don't register; no edit icon
- **Confidence:** M
- **Symptom:** Department + Target Gender empty in engine view despite Atlas correctly recognizing the styles as Women's in other surfaces (title generation, brand inference).
- **What's actually being written:** `preupload_importer.py:240, 280-281` writes both `department#1.value` and `target_gender#1.value`. So the writer is fine.
- **Likely cause:** Same key-mismatch family as Item 9. Engine view reader queries `department#1` or a brand-level singleton rather than the per-style field. No edit icon → field is rendered as read-only because no schema-mode is bound to it.
- **File:** Engine view template (in `app.py`), `substrate/field_suggest.py`.

---

### Pass 2 cluster — taxonomy stickiness

#### Item 1 — Item Type Keyword shows only Swimwear options regardless of Cat/Subcat
- **Confidence:** M
- **Symptom:** After a prior Volcom Mens Swim project, the next project (Tahari Outerwear) shows only swimwear ITK options at style-level taxonomy. Bulk-taxonomy view is fine.
- **Likely cause:** `/api/taxonomy` (`app.py:6811`) accepts an optional `?product_type=` filter. The style-level taxonomy UI is calling it with a stale `pt_filter` from the previous session (or with the wrong default when PT is UNKNOWN — see Item 2). When PT is UNKNOWN, the call probably defaults to the previous workspace's PT.
- **Fix sketch:** Frontend: clear `pt_filter` on workspace switch. Backend: when `pt_filter` doesn't match a row's actual PT, return the unfiltered universe instead of stale subset.

#### Item 2 — Atlas couldn't identify PT (UNKNOWN); operator manually chose Jackets & Coats; Cat/Subcat/ITK still don't populate
- **Confidence:** M
- **Symptom:** Manual PT selection doesn't re-fire the taxonomy cascade.
- **Likely cause:** Two separate state stores. The "manual PT override" is written to one place (probably `taxonomy_overrides` via `/api/taxonomy/save`) but the dropdown cascade reads from another (`product_type#1.value` on the row state). They don't sync until the row is re-evaluated.
- **Fix sketch:** On manual PT save, dispatch a state update that triggers cascade rebuild. Add a smoke test that asserts cascade options match PT after manual override.

#### Item 3 — At Bulk Taxonomy level, UNKNOWN PTs persist and don't flow taxonomy options after manual choice
- **Confidence:** M
- **Symptom:** Same as Item 2 but at the bulk-taxonomy view.
- **Likely cause:** Same state-sync problem. Likely fixed by the same change as Item 2.
- **Dependency:** Co-fix with Item 2.

#### Item 10 — Incorrect Item Length Description (might still be on DRESSES PT defaults); no fix button
- **Confidence:** M
- **Symptom:** Tahari coat's item length description is wrong; suspected stale default from a prior DRESSES project. No UI to fix.
- **What's actually being written:** `preupload_importer.py:297-298`:
  ```python
  "item_length_description#1.value":
      f"{s.get('length')}-inch" if s.get("length") else "",
  ```
  If template doesn't supply `length`, the field is blank, and a downstream default may fill in the wrong PT's expectation.
- **Fix sketch:** PT-specific default for item length (DRESSES uses inches; COAT uses category labels like Hip Length / 3/4 Length). Expose an editable input regardless of source.

#### Item 14 — Can't edit UNKNOWN taxonomy in some views
- **Confidence:** L
- **Symptom:** UNKNOWN taxonomy is locked from some views.
- **Likely cause:** UI guard that disables editing when PT is "UNKNOWN" — was added to prevent invalid Cat/Subcat picks when PT isn't set, but now blocks the operator from setting PT in the first place. Inverted gating.
- **File:** Engine view template.

---

### Pass 3 cluster — multi-value attributes

#### Item 11 (Material side) — Material should support multiple columns (Polyester | Cotton | Spandex)
- **Confidence:** H
- **Symptom:** Today only one Material value is supported. Amazon template uses up to 3 (Material 1, Material 2, Material 3).
- **What's needed:** Schema and state shape change. `material#1.value` becomes an ordered array of `{value, percentage}` tuples. UI exposes "Add another" chip.
- **File:** `nis_engine/preupload_importer.py` (parse multi), engine view (multi-value input), `substrate/asin_metadata.py` (write path).

#### Item 15 — Closure Type can't accept multiple values (e.g., Zipper + Snap)
- **Confidence:** H
- **Symptom:** Single-value picker today.
- **What's needed:** Same pattern as Material. `closure#1.type#1.value` becomes an ordered array.
- **Dependency:** Same Pass 3 work as Item 11.

---

### Pass 5 cluster — UI polish

#### Item 6 — Each style block is blacked out unless hovered
- **Confidence:** L
- **Symptom:** Style cards in the Styles section are dark/invisible until hover.
- **Likely cause:** CSS regression — likely a `:not(:hover)` rule applied opacity 0 or background-color black to the inactive state. Probably introduced in a recent dark-theme contrast pass (commit `7162149` "M6 UX pass 1: fix dark-theme contrast").
- **File:** Inline CSS in `app.py` near the NIS page rendering.

#### Item 7 — Bullets get `ALLCAPS + dash` even when `ALLCAPS + colon` already exists
- **Confidence:** H
- **Symptom:** Template bullet `ELEVATED WARMTH: The quilted puffer...` becomes `ELEVATED WARMTH: THE — quilted puffer...` (double-headlining).
- **What's actually wrong:** `nis_engine/content_rules.py:216-228` — the headline detector checks for ` — ` (em-dash) or ` - ` (hyphen-dash) but not `:` (colon). When input has `HEADLINE: rest`, the function falls through to the "derive headline" branch and creates a *new* ALLCAPS headline on top of the existing one.
- **Fix sketch:** Add `:` to the separator check. If `head.endswith(":")` already, treat the colon as the separator and skip the dash-injection branch.
- **Dependency:** None. Single-file fix.

#### Item 12 — All Fields > Content tab shows original generated content, not the edits
- **Confidence:** L
- **Symptom:** Operator edits a bullet in the Content tab; the All Fields > Content section still shows the original. Also redundant with the Content tab.
- **Likely cause:** All Fields view reads from `decision_event.atlas_output` (the original) instead of `operator_response.final_value` (the edited). Or it reads from a stale local-state cache.
- **Fix sketch:** Option A — fix the reader to prefer `operator_response.final_value` when present. Option B (the agency's suggestion) — delete the section entirely, since it duplicates the Content tab.
- **Recommendation:** Option B. Less code, less surface to maintain.

---

## Strategic asks — separate from the 15 items

### Strategic 1 — Multi-level edit scope ("apply to: just this / project / PT / brand")
- **Substrate readiness:** Already exists. `operator_response.scope` enum has `just_this / batch / brand_always / propose_rule`. `scope_keys` (asin, family, decision_class) drive Loop 1 promotion.
- **Gap:** UX surfacing. Currently only NIS field corrections + rule promotion expose the scope picker. Every editable structured attribute needs to expose it too.
- **Pass 4 deliverable:** Reusable `<ScopePicker>` component, wired on Department first, then rolled across Material, Closure, Sleeve Length, Item Length, Department, Target Gender.

### Strategic 2 — Pre-upload template v2
- **Current shape (v1, 31 cols):** Mixes TLG-internal (TLGDIV NAME, Brand Code as separate, TLG Style Desc) with Amazon-attribute columns. Multi-value attrs collapsed to single columns.
- **v2 shape (target, ~27 cols):** Amazon-attribute-shaped only.
  - **Drop:** TLGDIV NAME, TLG Style Desc, MODEL NAME (redundant with STYLE NAME), CHILD ASIN (move to optional "Existing Listing" lookup), SKU (auto-derived).
  - **Add:** Department, Age Range, Target Gender, Item Length Description, Sleeve Length, Material 2, Material 3, Closure 2.
  - **Restructure:** Fabric Content Percentage stays; Material becomes 1/2/3.
- **Migration:** v1 importer kept; on parse, fields are auto-mapped to v2 internal shape. New uploads use v2 directly.
- **Pass 6 deliverable:** New template xlsx + ingest mapping layer + dual-format acceptance.

---

## Dependency graph (which fixes unblock which)

```
Pass 0 — this doc + fixture + qa skeleton
   │
   ▼
Pass 1 — parser key-name alignment + verdict accuracy
   │   [unblocks 8 of 15 items if hypothesis is right]
   ▼
Pass 2 — taxonomy state-sync (PT/Cat/Subcat/ITK)
   │
   ▼
Pass 3 — multi-value attribute schema + UI
   │
   ▼
Pass 4 — scope-aware editing UI (the leverage pass)
   │
   ▼
Pass 5 — cosmetic polish (hover, bullet formatter, content tab)
   │
   ▼
Pass 6 — pre-upload template v2
```

Passes 1, 2 can run in parallel if needed. Pass 3 needs Pass 1 (parser fixes) before it lands. Pass 4 needs Passes 1-3 because it touches the same field-rendering paths.

---

## Open questions (to resolve before Pass 1 begins)

1. **Item 5 — verdict false blockers — is this a single root cause or two separate bugs?** Need to run the Tahari fixture through `evaluate_form` locally and dump verdict vs state. If verdict and state disagree on field key names, it's one bug (Pass 1 single fix). If they agree but verdict logic itself is wrong, it's a separate bug.
2. **Item 11 — Material parsing: do we always have a percentage in the fabric string?** Some rows in the Tahari fixture have `100% Polyester`; others might say `Polyester` (no %). Parser needs to handle both.
3. **Item 6 — which CSS commit introduced the hover blackout?** `git bisect` between `7162149` (M6 UX pass 1) and current master will tell us. Defer until Pass 5.

---

## Version history

Append below this line. Do not edit entries above.

- **v1.0 — 2026-06-02, present commit** — Pass 0 deliverable. Fifteen issues mapped against code paths with confidence ratings. Strategic asks separated from item fixes. Pass-by-pass sequence locked. Fixtures checked in at `data/fixtures/tahari_r0_*.xlsx`. QA harness skeleton shipped as `qa_agency_r0.py`. Next: Pass 1 awaiting operator sign-off.
