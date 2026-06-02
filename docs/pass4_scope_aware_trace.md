# Pass 4 — Scope-aware editing · Runtime Trace

> Agency R0 sprint, Strategic 1. Surface the substrate's `operator_response.scope` enum across every editable structured attribute so a `brand_always` edit on style #1 reads through to style #2 in the same session.
>
> Substrate: `substrate/schema.py` — `OperatorScope.{JUST_THIS, BATCH, BRAND_ALWAYS, PROPOSE_RULE, NONE}`. Already there. Pass 4 wires the UI + read-through path.

---

## What the substrate already had

`OperatorScope` enum lives in `substrate/schema.py:80-93`. `update_field_decision_with_operator_response` in `substrate/logger.py:488` already accepts a scope arg and writes it into the decision-event log. The `/api/atlas/decision-response` endpoint at `app.py:13900` accepts `scope` in its payload and forwards to the logger.

What was missing: **session-level read-through**. The logger writes to a substrate database for downstream Loop 1 promotion. But the wizard renders the next style by calling `style_to_form_state(style, brand)` — a pure function with no awareness of any prior in-session edits. Strategic 1's ask is the read-through, not more substrate plumbing.

---

## The change in three layers

### 1. `style_to_form_state` takes optional override maps

`nis_engine/preupload_importer.py`. Function signature gains two kwargs:

```python
def style_to_form_state(
    style, brand,
    scope_overrides: Optional[Dict[str, Any]] = None,  # brand_always store for this brand
    batch_overrides: Optional[Dict[str, Any]] = None,  # session-global store
) -> Dict[str, Any]:
```

After the template-derived state is built, two override layers are applied (lowest precedence first):

```
template/auto-derive  <  batch  <  brand_always  <  just_this (applied by caller)
```

Empty / None override values are skipped — they must never clobber valid template content. That's defended by `test_pass4_scope_brand_always_reads_through`'s last assertion.

### 2. Session storage + write endpoint

`app.py` `session_data` gains two new fields:

```python
"scope_overrides": {},   # {brand: {field_key: value}}
"batch_overrides": {},   # {field_key: value}
```

`POST /api/atlas/field-edit-scoped` accepts `{ brand, field_key, value, scope, style_num }` and routes:

| scope | store | affected_styles count |
|---|---|---|
| `just_this` | `field_overrides[style_num][field_key]` | 1 |
| `batch` | `batch_overrides[field_key]` | all styles in session |
| `brand_always` | `scope_overrides[brand][field_key]` | all brand styles |
| `propose_rule` | `proposed_rules[]` (append-only list) | 0 — Loop 1 handles promotion |

`GET /api/atlas/scope-overrides` returns the full session view for UI badge rendering.

### 3. `import-preupload` consults the override stores

The single-line change at the loop body in `rule_engine_import_preupload`:

```python
state = style_to_form_state(
    style, brand,
    scope_overrides=_brand_scope_ovs,
    batch_overrides=_batch_ovs,
)
# layer just_this on top (highest precedence)
for k, v in (session_data["field_overrides"].get(str(style_id)) or {}).items():
    if v not in (None, "", " "):
        state[k] = v
```

That's the read-through. Every subsequent `/import-preupload` call (which the wizard fires on re-evaluate) picks up the latest session edits.

---

## Frontend — `<ScopePicker>` + chip multi-input

Three reusable helpers added at the top of the page module (after `showToast`):

- **`scopePickerHTML(label, currentValue, styleNum, fieldKey)`** — returns inline HTML for a small `[just this style ▾]` chip select with the four scope options. Drop next to any editable input.
- **`scopePickerCommit(fieldKey, value, styleNum, scopeEl)`** — POSTs to `/api/atlas/field-edit-scoped` with the chosen scope, shows a toast that calls out the read-through ("Saved — will read through to all 23 Tahari styles" for `brand_always`).
- **`multiChipHTML(label, values, fieldKeyPrefix, validValues, styleNum)`** — Pass 3 carried forward. Chip strip with `×` to remove and a `+ add` select to add from PT-valid values. Each chip commit hits the scope-aware endpoint with default scope `just_this`.

**Honest limit on the chip multi-input:** add and remove call `scopePickerCommit` to persist, but the in-strip DOM mutation is best-effort — the strip doesn't re-fetch the updated valid-values set between mutations. A second `+ add` after a first add will reflect stale `validValues`. Real fix requires the parent re-render to flow new state in. Production rollout will need a `/api/atlas/field-clear` endpoint for explicit slot clears (today, removal commits empty string which the importer's override layer correctly ignores — so removed chips actually do not propagate yet).

---

## What's been validated

- Backend: a brand_always store applied to two different styles produces the right state on both — assertion lives in the QA harness.
- Backend: empty overrides don't clobber template values.
- Backend: batch + brand_always layers compose without colliding.
- Backend: just_this overrides apply at the loop body, not inside `style_to_form_state`, so they win over both other stores.

What is **not** validated:
- The ScopePicker chip is not yet attached to any specific input in the existing wizard markup. The helpers are global; rollout to Department, Material, Closure, Sleeve Length, Item Length, and Target Gender is a follow-up — each insert is two lines (inline the chip + wire the input's onblur handler) but each one is a separate place in `templates/index.html` and benefits from in-browser smoke. That's intentionally not bundled into Pass 4.
- No Playwright assertion for the chip UI yet (same reason as Pass 2 frontend fixes — would need a separate test rig).

---

## Bias-to-flag (carried forward)

Pass 4 is the first pass in this sprint where we're building substantial new UX. The substrate enum has been there for months without read-through wiring. Two ways to interpret that: (a) the team was waiting for the right surface, or (b) it was over-engineered upfront. Atlas should flag bias toward "more building" in the architecture doc rather than silently extending. Read-through is the minimum that satisfies the agency's request; everything beyond that (UI rollout across all fields, propose-rule promotion pipeline, scope-aware writer for the xlsm output) is opt-in for future passes.

---

## QA state

After Pass 4: **17 pass · 0 fail · 5 pending** (Pass 5 cosmetic, Pass 6 template v2).
