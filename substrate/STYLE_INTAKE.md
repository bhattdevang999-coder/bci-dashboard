# STYLE_INTAKE — Module Specification

> New substrate module introduced in CONTINUOUS_LEARNING_ARCHITECTURE.md v1.3 (June 8, 2026). Sits upstream of NIS in the listing-creation pipeline. Permanent — versioned at the bottom, never rewritten in place.

This file is the source of truth for what STYLE_INTAKE is, what it captures, what its v1 MVP scope is, and what is deferred to later versions. When someone asks "is STYLE_INTAKE done?" or "does it support X?", point them here.

---

## Why this module exists

The listing-creation pipeline today has five hand-offs from a style being designed to a finished Amazon listing:

1. Designer's brain → designer's internal Excel
2. Internal Excel → factory tech pack
3. Factory tech pack → packing list
4. Packing list → preupload template (the existing rules engine input)
5. Preupload template → NIS wizard → Amazon listing

Each hand-off loses fidelity. Colors get renamed. Fabric types get misspelled. Closure types get dropped. Item lengths get defaulted. The downstream firefighting in the NIS wizard (Pass 1 through Pass 6 of the Agency R0 sprint, mostly) is fixing problems that should not have existed in the first place — because the data was wrong before it ever entered the rules engine.

STYLE_INTAKE collapses hand-offs 1, 2, and 4 into a single direct entry surface. The designer enters the style once, in Atlas, with PT-aware validation and smart pre-fill from similar past styles. The output replaces the preupload Excel as the input to the NIS wizard.

This is not a new learning loop. It is a new ingestion surface that produces earlier, denser, lower-noise signal for the existing Loop 1 (NIS preference model). See CONTINUOUS_LEARNING_ARCHITECTURE.md §Loop 1 for the data-plumbing details.

---

## v1 MVP — scope and non-scope

### In scope for v1

- **Single-brand sessions.** Operator picks one brand at session start. All styles entered in that session belong to that brand. No mixing.
- **Form-based entry, one style at a time.** Each style is a separate form submission. No batch mode in v1.
- **PT-aware required fields.** The form dynamically shows fields based on the selected product type. Required vs recommended vs optional fields are visually distinguished. Field schema is read from the existing `field_schema.yml` (no new schema source for v1).
- **Pre-fill from same-brand + same-sub_class history.** When a designer enters a new style with the same brand and sub_class as past styles, Atlas pre-fills the fields that have stable values across those past styles. Single similarity signal — brand + sub_class only. No fuzzy match. No embedding similarity.
- **Tick-mark confirmation per pre-filled field.** Every pre-filled value must be explicitly ticked by the designer before submit. Untouched pre-filled fields keep the form in a "not yet submittable" state.
- **Override surfaces a clear UI gesture.** If the designer disagrees with a pre-filled value, they edit it directly — that edit auto-marks the field as confirmed AND records the pre-fill source as "wrong for this style." This is the highest-quality training signal.
- **Output writes to `style_intake` table.** Downstream NIS preupload generation reads from this table.
- **Substrate writes** — every pre-fill suggestion writes a `decision_event` (the suggestion itself is a decision). Every confirm or override writes an `operator_preference_pair` (feeds Loop 1).

### Explicitly NOT in v1

- **Multi-brand workflows.** Defer to v2. The brand picker constraint is a deliberate scope cut — building cross-brand support introduces field-schema reconciliation problems that are not worth solving for MVP.
- **Bulk paste / Excel import.** Defer to v2. The "paste from Excel" power-user flow is real demand but adds parsing complexity. v1 ships form-only.
- **Embedding-based similarity for pre-fill.** Defer to v3 or later. No semantic similarity search across product descriptions. v1 uses exact brand + sub_class match only.
- **Versioning history.** Defer to v2. v1 is "last write wins" — when a designer re-enters a style, the previous row is overwritten. Edit history is captured in the audit log via `decision_event` writes, but the form UI does not surface previous versions.
- **Agency-facing access controls.** Defer to v2. v1 assumes single-tenant operator access (Devang's team). Agency access requires per-brand permissioning that we are not building yet.
- **Photo / asset attachment.** Defer to v2 or integrate with M7 Creative Studio (already shipped). v1 is text fields only; designers attach photos through the existing image library workflow.
- **Inline factory tech pack parsing.** Defer to v2 or later. No "upload your tech pack PDF and Atlas extracts the fields." v1 is manual entry.

---

## Schema — `style_intake` table (proposed for v1 deliverable)

| Column | Type | Notes |
|---|---|---|
| `intake_id` | uuid | PK |
| `workspace_id` | text | FK to workspace; required |
| `brand_name` | text | Required; constrained to existing brand_kit values |
| `style_number` | text | Required; unique within (workspace_id, brand_name) |
| `feed_product_type` | text | Required; one of shirt, shorts, pants, etc. |
| `sub_class` | text | Required; the existing rules-engine sub_class taxonomy |
| `style_name` | text | Required; designer-facing name (becomes basis of `item_name`) |
| `fields_json` | jsonb | All other style attributes (fabric_type, closure_type, item_length_description, color names, size system, material composition, etc.) — schema-validated against `field_schema.yml` per PT |
| `confirm_state_json` | jsonb | Per-field state: confirmed_unchanged / overridden / pending. Required fields must all be in non-pending state for submission. |
| `pre_fill_sources_json` | jsonb | Per-field provenance — which past style(s) contributed to the pre-filled value |
| `created_by` | text | Designer identifier |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |
| `submitted_for_nis` | boolean | Whether this row has been promoted into the NIS preupload generator yet |

The schema is intentionally narrow. `fields_json` carries the long tail of PT-specific attributes; we do not want to add columns for every possible apparel attribute, and the validation is enforced at write time by `field_schema.yml`.

### Decision events written by STYLE_INTAKE

For each new style entered:

- **One `decision_event` per pre-filled field** with `decision_class='style_intake_pre_fill'`, `payload={field_name, pre_filled_value, pre_fill_source}`. The decision event is written at the moment Atlas computes the pre-fill, before the designer sees it.
- **One `operator_preference_pair` per ticked or overridden field** with `decision_class='style_intake_field'`, `original=pre_filled_value`, `edited=confirmed_value`, `scope_keys={brand, sub_class, field_name}`. Written at submit time. Feeds Loop 1 (see CONTINUOUS_LEARNING_ARCHITECTURE.md §Loop 1).

---

## UX — the form

The form sits at a new sidebar item: `Inputs → Style Intake` (placement subject to UX review with the team).

### Entry flow

1. **Brand picker.** Operator selects one brand for the session. The picker shows the brands the workspace has access to. Once selected, the picker locks for the rest of the session.
2. **PT and sub_class picker.** Operator selects the product type (shirt / shorts / pants / etc.) and the sub_class. These two together determine the field schema for the rest of the form.
3. **Style identity fields.** Style number, style name (the designer-facing label). Required, no pre-fill.
4. **Pre-fill calculation.** Atlas queries past styles with the same brand + sub_class. For each field that has a stable value across those past styles (e.g. fabric_type = "Cotton Blend" for 80% of past styles), the field is pre-filled with that stable value. For fields with no stable past value, the field stays empty.
5. **Form rendering.** All fields shown in PT-aware order. Each field has one of three visual states:
   - **Empty (required)** — outlined in red, no value, designer must enter
   - **Empty (optional)** — outlined neutral, no value, designer may enter or skip
   - **Pre-filled (unconfirmed)** — value shown in normal text weight, has a small unticked checkbox to the right, gold/amber visual cue
   - **Confirmed** — value shown, checkbox ticked, normal visual treatment
6. **Designer actions.** Tick a checkbox to confirm a pre-filled value. Or edit the value directly — editing auto-confirms (you can't override and still leave it unconfirmed). Or leave a non-required field empty.
7. **Submit gate.** Submit button is disabled until every required field is in a confirmed state. Hover-over Submit when disabled shows which fields are still pending.
8. **Submit action.** Writes the `style_intake` row plus all the substrate events. Returns the designer to the brand-picker step (or to a "next style" continuation).

### Visual treatment of pre-filled values — the tick-mark UX

This is the part of the UX worth getting right. The pattern is:

- Pre-filled values display in **normal text weight** (not greyed out) so the designer actually reads them. Greyed-out pre-fill leads to "click through without reading" behavior.
- Each pre-filled field has a small **unticked checkbox** immediately to the right of the value.
- The checkbox label reads "Confirm" (not "OK" or "Yes" — "Confirm" implies an active gesture).
- On hover, the checkbox shows a tooltip: "This was pre-filled from {N} past styles. Confirm if correct, or edit the value."
- Once ticked, the checkbox becomes a small green checkmark and the field's outline softens.
- Editing the field value clears the pre-fill state AND auto-ticks the field as confirmed (with the override flag set internally).

The reason for the explicit tick gesture: the action of ticking forces the designer's eye to the value. Without that gesture, pre-filled values get rubber-stamped, which is exactly the failure mode the architecture doc names as "pre-fill false confidence."

---

## Pre-fill logic — v1 implementation

For each (brand, sub_class) on entry:

1. Query `style_intake` for all past styles matching brand + sub_class. Call this set `S`.
2. If `|S| < 3`, no pre-fill (insufficient signal). All fields render empty.
3. For each field in the PT schema:
   a. Compute the most common value across `S` for that field.
   b. If the most common value covers ≥ 60% of `S`, pre-fill with that value.
   c. Otherwise, leave the field empty (signal is too noisy to suggest).
4. Record the pre-fill source per field — which subset of `S` contributed.

The 60% threshold and the |S| ≥ 3 minimum are heuristic. We tune them based on observed designer override rates: if designers override pre-filled values more than 40% of the time, the threshold is too low (we're suggesting too aggressively). If they confirm > 95%, the threshold is too high (we're under-suggesting).

No ML in v1. Frequency table only. The Field-default prior model described in CONTINUOUS_LEARNING_ARCHITECTURE.md §Loop 1 is a v2 deliverable — it requires enough STYLE_INTAKE history to learn from, which we won't have until ~Month 4.

---

## Integration with downstream NIS

Today, NIS preupload generation reads from an uploaded Excel template. After STYLE_INTAKE v1 ships, NIS preupload generation reads from the `style_intake` table for styles that have been entered via the new flow.

The transition is dual-path for the first ~3 months:

- Excel-based preupload still works (backwards compatible — Sheik's team and external agencies don't have to switch immediately)
- STYLE_INTAKE-based preupload is the recommended path for new styles entered by Devang's team
- The NIS preupload generator reads from whichever source is populated for a given style; if both are populated, `style_intake` wins

After ~3 months of dual operation, we evaluate whether Excel-based preupload can be deprecated. The honest expectation: it can't, because external agencies will continue to send Excel. So Excel-based preupload stays as the fallback ingestion path indefinitely. STYLE_INTAKE is for the in-house workflow.

---

## Failure modes (worth pre-naming)

1. **Designer adoption stalls.** If Devang's design team continues to use Excel because the form is slower for their workflow, STYLE_INTAKE sits unused. Mitigation: build with one or two designers in the room, not for them. First-month adoption check: at least 50% of new styles entered via STYLE_INTAKE, not Excel.
2. **Pre-fill becomes a footgun.** Designers tick without reading, false confidence enters the system. Mitigation: the tick UX described above. Also: a sampling audit by an operator (Devang) on a random subset of submitted styles in the first month catches systematic pre-fill errors.
3. **Schema drift between STYLE_INTAKE and rules engine.** As Amazon updates PT field requirements, the rules engine schema changes. STYLE_INTAKE has to stay in sync. Mitigation: single source of truth (`field_schema.yml`) for both surfaces — same file, no duplication.
4. **Agency demand for v1 access.** Sheik or other agency teams may want STYLE_INTAKE access before v2's multi-brand support is ready. Mitigation: explicit "single-brand-per-session" constraint is documented; agencies can still use Excel ingestion in the interim.
5. **Pre-fill source attribution becomes wrong over time.** As the past-style pool grows, the (60% threshold, |S| ≥ 3) heuristic may stop being right. Mitigation: monitor designer override rate as a health signal; tune thresholds when override rate drifts outside the 5-40% comfort band.

---

## Build sequence

| Phase | Scope | Duration estimate | Owner |
|---|---|---|---|
| **Scoping** | One-week conversation with at least one designer + Sheik. Confirm the form schema, the brand picker constraint, the tick UX, and the integration point with NIS preupload. | 1 week | Devang |
| **Phase 1 — Schema + storage** | Create `style_intake` table. Wire substrate event writes (`decision_event` for each pre-fill, `operator_preference_pair` for each confirm/override). | 3-4 days | Eng |
| **Phase 2 — Form UI** | Build the brand picker, PT picker, sub_class picker, field rendering, tick-mark UX, submit gate. No pre-fill yet — just the form with empty fields. | 5-7 days | Eng |
| **Phase 3 — Pre-fill logic** | Implement the frequency-based pre-fill described above. Wire into the form so pre-filled values render with the tick-mark UX. | 3-4 days | Eng |
| **Phase 4 — NIS integration** | Update NIS preupload generator to read from `style_intake` table. Dual-path with Excel ingestion. | 2-3 days | Eng |
| **Phase 5 — Daily-use validation** | One designer uses STYLE_INTAKE for all new Novelle styles for 2 weeks. Tune the 60% pre-fill threshold based on override rate. Document any UX friction. | 2 weeks (calendar) | Devang + designer |

**Total time to first daily-use validation: ~4-5 weeks of focused work.** Reality is probably 6-8 weeks calendar with normal operational interruptions.

---

## Version history

- **v1.0 — 2026-06-08, present commit** — Initial module specification. Covers v1 MVP scope (single-brand sessions, form-based entry, PT-aware fields, frequency-based pre-fill from brand+sub_class match, tick-mark confirmation, dual-path NIS integration). Explicitly defers multi-brand, bulk paste, embedding similarity, versioning, agency access controls, photo attachment, and tech-pack parsing to v2+. Schema sketch for `style_intake` table proposed; final schema commit is a v1 MVP deliverable, not part of this spec doc. Build sequence: 5 phases, ~4-5 weeks focused work + ~6-8 weeks calendar. Authored by Devang + Atlas after a v1.0 → v1.3 architecture doc update that integrates STYLE_INTAKE as a new ingestion surface feeding Loop 1.
