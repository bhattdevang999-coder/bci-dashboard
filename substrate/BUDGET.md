# Budget Substrate — Design Notes

> Layer: `substrate/budget.py` + `schema.sql` v4 + 4 HTTP endpoints under `/api/atlas/marketing/budget/*`
> Phase: 1 (live at SHA `4f22223`, 2026-05-16)
> Scope: **Strictly PPC.** Operational costs (photography, A+ design, content rewrites) are out of scope for v1.

This file is the permanent design record for budget tracking. Schema migrations bump the version number at the bottom; do not rewrite this document in place.

---

## Why this exists

Atlas already captures every operator decision (Memory) and every keyword observation (Marketing substrate). What was missing: a place to record **what the operator planned to spend** so we can compare it against what actually happened. Without planned amounts, "ACOS went up 15%" is just a number — you can't tell whether the operator overshot a budget, underfunded a launch, or had a content change confound the spend signal.

Budget is the third leg of the closed-loop attribution stool:

| Layer | Question it answers |
|---|---|
| Marketing substrate (`keyword_library`, `outcome_events`) | What did spend / clicks / ACOS look like for each keyword? |
| Memory (`substrate_events`) | What did the operator change on the listing? |
| **Budget** (this layer) | **What did the operator plan to spend, and by how much did they miss?** |

The closed-loop attribution layer (Phase 2) joins all three.

---

## Schema (v4 migration)

```sql
CREATE TABLE budget (
    workspace_id   TEXT        NOT NULL,
    period         TEXT        NOT NULL,                    -- 'YYYY-MM'
    scope_type     TEXT        NOT NULL
                                CHECK (scope_type IN ('theme', 'overall', 'asin')),
    scope_value    TEXT        NOT NULL,                    -- theme name | '_overall' | ASIN
    amount         NUMERIC(12, 2) NOT NULL CHECK (amount >= 0),
    currency       TEXT        NOT NULL DEFAULT 'USD',
    set_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    set_by         TEXT,                                    -- operator_id
    notes          TEXT,
    meta           JSONB DEFAULT '{}'::jsonb,
    PRIMARY KEY (workspace_id, period, scope_type, scope_value)
);
```

**Granularity v1:** one row per (workspace_id, period, scope_type, scope_value).

- `period` = `YYYY-MM` (e.g. `'2026-05'`). Month is the only supported grain in v1 — daily is too noisy for the long-tail keyword spend reality, quarterly is too coarse for course correction.
- `scope_type ∈ ('theme', 'overall', 'asin')`. The `asin` scope is **already wired through the substrate and validators** — v1 UI just doesn't expose the path yet. When we want per-ASIN budgets in v2, no migration is required.
- `scope_value`:
  - for `theme`: `'branded' | 'feature' | 'competitor'`
  - for `overall`: the literal string `'_overall'`
  - for `asin`: `B0XXXXXXXX`

**Why a current-state table and not just events.** Every budget set or revise also writes a `decision_event` (see "Audit trail" below) — that's the append-only history. The `budget` table is the rolled-up *current state* projection so reads stay fast. If the projection ever diverges from the events, the events are the source of truth.

---

## Status ladder

The single most important user-visible concept. Every variance row carries one of these statuses:

| Status | Condition | When it fires |
|---|---|---|
| `no_data` | planned is None/0 AND actual is 0 | Period has no plan and no spend yet — show as blank, not as a failure. |
| `no_budget` | planned is None/0 AND actual > 0 | Spend exists but no plan was set. **Flag visibly** — this is the failure mode that destroys attribution. |
| `no_spend` | planned > 0 AND actual is 0 | Plan exists but no spend has hit yet. Mid-month: probably fine. End of month: investigate. |
| `under` | `actual / planned ≤ 0.95` | Came in at or below 95% of plan. |
| `at` | `0.95 < actual / planned ≤ 1.05` | Within ±5% of plan. This is the target state. |
| `over` | `actual / planned > 1.05` | Exceeded plan by >5%. |

The 95% / 105% bands are deliberate: tighter and every theme reads "over" by month-end (PPC has a multi-day attribution tail); looser and we lose signal. We will revisit these once we have 3 months of variance history per brand.

Computed by `_bucket_status(planned, actual)` in `substrate/budget.py`. Floating-point boundaries are handled in that function — do not duplicate the comparison logic elsewhere.

---

## Theme attribution via LATERAL join

The hard problem: `outcome_events.keyword` carries the raw keyword string the PPC report logged, but it has **no theme column**. The theme lives on the `decision_event` Atlas wrote when the marketing wizard proposed that keyword (in `atlas_output.theme`).

So `variance_for_period` resolves theme attribution at query time:

```sql
SELECT
    COALESCE(d.atlas_output->>'theme', 'unknown') AS theme,
    COALESCE(SUM(o.value), 0) AS spend
FROM outcome_events o
LEFT JOIN LATERAL (
    SELECT atlas_output
    FROM substrate_events
    WHERE workspace_id = o.workspace_id
      AND event_kind = 'decision_event'
      AND module = 'marketing'
      AND field_name = 'keyword_candidate'
      AND LOWER(atlas_output->>'keyword') = LOWER(o.keyword)
    ORDER BY timestamp DESC
    LIMIT 1
) d ON TRUE
WHERE o.workspace_id = %s
  AND o.metric = 'spend'
  AND o.observed_at >= %s AND o.observed_at < %s
  AND o.keyword IS NOT NULL
GROUP BY 1
```

**Why LATERAL, not a normal JOIN.** A keyword may have been proposed many times across different sessions, possibly with different themes. We want the **most-recent** decision_event for that keyword as the truth source — that requires a per-row subquery, which is exactly what LATERAL is for. A naive JOIN would either duplicate spend (once per matching decision event) or pick an arbitrary theme.

**The unknown bucket.** Spend whose keyword has no marketing decision_event rolls up to `theme:unknown`. **This will dominate variance until the wizard has been used for the majority of paid keywords**, because the LATERAL subquery only resolves themes for keywords Atlas itself proposed. Until we either (a) ingest enough wizard runs to cover the long tail, or (b) build a fallback classifier, the budget UI must show "unknown" prominently and honestly. Do not collapse or hide it.

**Case sensitivity.** Both sides are `LOWER()`-normalized to match the wizard's normalization (`normalise_keyword` in `marketing.py`). Anything that touches keyword-comparison logic must match this.

---

## Content-change markers

PPC spend variance is not a clean signal. If you "overspent" branded but also rewrote three product titles in the same month, you can't attribute the variance cleanly. The conservative answer: **mark every period where content changed on a spending ASIN, and surface those markers on every variance row.**

A content-change marker is a `decision_event` with `module='nis'` on an ASIN that had `metric='spend'` outcome rows in the same period:

```sql
SELECT meta->>'asin' AS asin,
       COUNT(*) AS n_decisions,
       MAX(timestamp) AS last_change_at
FROM substrate_events
WHERE workspace_id = %s
  AND event_kind = 'decision_event'
  AND module = 'nis'
  AND timestamp >= %s AND timestamp < %s
  AND meta->>'asin' = ANY(%s)         -- ASINs that had spend in the period
GROUP BY 1
```

This is **strictly a marker, not a budget item.** The user agreed: content changes do not consume budget, they annotate it. The variance row stays calculated against PPC spend; the markers tell the operator "before drawing conclusions from this row, note that ASIN X had 4 listing changes in this period."

On the variance API response, `content_changes` appears on:

- per-ASIN scope rows: only the markers for that ASIN
- theme / overall rows: all touched-ASIN markers, because spend at those scopes mixes ASINs

`content_changes_summary` at the top level gives the bird's-eye: `n_asins_touched_by_spend` vs `n_asins_with_content_changes`. The ratio is the operator's confidence ceiling on the entire month's attribution.

---

## Audit trail: every budget edit is a decision_event

Budget revisions land in Memory the same way listing decisions do. On every `set_budget` call:

```python
log_field_decision(
    workspace_id=workspace_id,
    session_id=None,                        # standalone, not session-wrapped
    module=Module.BUDGET,                   # 'budget'
    field_name="monthly_allocation",
    atlas_output={
        "period": period,
        "scope_type": scope_type,
        "scope_value": scope_value,
        "amount": amount_f,
        "currency": currency,
        "notes": notes,
    },
    overall_confidence=1.0,                 # operator-initiated, always certain
    rules_injected=[],
    brand_profile_version=f"{workspace_id}_legacy",
    enforce_filter=False,                   # bypass the noise filter
)
```

**Why no session.** Budget edits are quick standalone actions. Wrapping each click in a session would create noise sessions with one decision each. If a future budget UI ever batches multiple revises into a planning ritual, that's the time to introduce a session.

**`delete_budget` does not delete the audit trail.** It removes the projection row only. The original `decision_event` stays in `substrate_events` forever. This is the same principle as all substrate writes: history is append-only.

**Why `private_scope=True` (the default).** Budget figures are workspace-private and never feed the global rule library. They're operator decisions about their own brand, not generalizable patterns.

---

## API surface

All endpoints sit under `/api/atlas/marketing/budget` because budgets are PPC-scoped in v1 and live as a sub-tab inside the Marketing UI.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/atlas/marketing/budget?period=YYYY-MM` | List budgets, optionally filtered to one period. |
| `POST` | `/api/atlas/marketing/budget` | Upsert (insert or revise). JSON body: `{period, scope_type, scope_value, amount, currency?, notes?}`. |
| `DELETE` | `/api/atlas/marketing/budget?period=...&scope_type=...&scope_value=...` | Remove a budget row. Audit-trail decision_events stay. |
| `GET` | `/api/atlas/marketing/budget/variance?period=YYYY-MM` | Planned-vs-actual for the period — scopes, totals, content-change summary. |

All return `{ok: bool, ...}`. Validation failures return HTTP 400 with `{ok: false, error: "..."}`. The variance endpoint is read-only and never raises (worst case: returns a well-formed response with empty data).

---

## Response shape: `GET /variance`

```json
{
  "ok": true,
  "workspace_id": "novelle",
  "period": "2026-05",
  "period_start": "2026-05-01T00:00:00+00:00",
  "period_end":   "2026-06-01T00:00:00+00:00",
  "scopes": [
    {
      "scope_type": "overall",
      "scope_value": "_overall",
      "planned": 1500.0,
      "actual":  1400.0,
      "delta":   -100.0,
      "pct_used": 0.9333,
      "status":  "under",
      "currency": "USD",
      "notes":    null,
      "content_changes": [
        { "asin": "B0AAA00001", "n_decisions": 2, "last_change_at": "2026-05-08T..." }
      ]
    },
    {
      "scope_type": "theme",
      "scope_value": "feature",
      "planned": 500.0,
      "actual":  600.0,
      "delta":   100.0,
      "pct_used": 1.2,
      "status":  "over",
      "currency": "USD",
      "notes":    null,
      "content_changes": [ ... ]
    },
    {
      "scope_type": "theme",
      "scope_value": "unknown",
      "planned": null,
      "actual":  550.0,
      "delta":   null,
      "pct_used": null,
      "status":  "no_budget",
      "currency": "USD",
      "notes":    null,
      "content_changes": [ ... ]
    }
  ],
  "totals": {
    "planned":  1500.0,
    "actual":   1400.0,
    "delta":    -100.0,
    "pct_used": 0.9333,
    "status":   "under"
  },
  "content_changes_summary": {
    "n_asins_touched_by_spend": 3,
    "n_asins_with_content_changes": 1
  }
}
```

**Totals roll-up rule.** If an `overall:_overall` scope row exists for the period, totals echo it directly. Otherwise, totals sum the `theme:*` rows. This avoids double-counting: when an overall plan is set, themes are sub-allocations of it, not additions.

---

## Validation

`set_budget` rejects (returns `{ok: false, error: ...}`):

| Input | Rejection reason |
|---|---|
| `period` not matching `^\d{4}-\d{2}$` | `"period must be YYYY-MM"` |
| `period` year outside 2020–2100 or month outside 1–12 | same |
| `scope_type` not in `('theme', 'overall', 'asin')` | `"scope_type must be one of ..."` |
| `scope_value` missing | `"scope_value required"` |
| `scope_type='theme'` and `scope_value` not in `('branded', 'feature', 'competitor')` | `"theme scope_value must be one of ..."` |
| `scope_type='overall'` and `scope_value != '_overall'` | `"overall scope_value must be '_overall'"` |
| `scope_type='asin'` and `scope_value` doesn't match `^B0[A-Z0-9]{8}$` | `"asin scope_value must look like B0XXXXXXXX"` |
| `amount` not numeric | `"amount must be a number"` |
| `amount < 0` | `"amount must be >= 0"` |

Validation happens **before** any DB write, so an invalid budget never reaches the audit trail.

---

## What's deliberately out of scope (v1)

- **Operational costs.** Photography, A+ rewrites, agency retainers. They're real money but they don't drive PPC variance, and mixing them in would muddy the closed-loop signal.
- **Sub-monthly granularity.** Daily / weekly. PPC has a multi-day attribution tail that makes anything tighter than a month statistically noisy on small budgets.
- **Multi-currency at the same scope.** A row is one amount in one currency. If a brand operates in multiple regions, they live in separate workspaces.
- **Forecasting.** Variance compares actual to planned. We don't *predict* future actual from past data here — that's an analytics layer concern, not a substrate concern.
- **ASIN-scope UI.** Substrate supports `scope_type='asin'`; the v1 Budget tab does not expose it. Add the UI when the operator demand is real, not preemptively.

---

## Tests

`/tmp/qa_budget.py` — 64 assertions covering:

- Schema: Module.BUDGET, budget table exists, v4 migration row present
- Validators: period format, scope_type, scope_value, amount type/sign
- `_period_to_range`: month boundaries including December rollover
- `_bucket_status`: every transition in the status ladder, including 95%/105% boundaries
- `set_budget`: insert path, revise path, all validation rejections
- `list_budgets`: empty workspace, period filter, ordering (period DESC)
- `delete_budget`: happy path, invalid scope, double-delete idempotence
- Audit trail: every set_budget writes exactly one decision_event with `module='budget'`, `field_name='monthly_allocation'`
- `variance_for_period`: empty period, planned-only (no_spend), actual-only (no_budget), full ladder (under / at / over / unknown bucket)
- Content-change markers: in-period vs out-of-period filtering, ASIN matching
- Workspace isolation: one workspace cannot see another's budgets

Regression suites that must stay green when budget changes:

- `qa_inputs.py`, `qa_memory_be.py`, `qa_memory_decisions.py`
- `qa_marketing_substrate.py`, `qa_wizard.py`
- `substrate/test_db.py`, `test_schema.py`, `test_logger.py`, `test_judgment.py`

Live smoke checks added to `scripts/verify_deploy.sh`: `marketing/budget` (list) and `marketing/variance` (variance with a default period).

---

## Version history

Append below this line. Do not edit entries above.

- **v1.0 — 2026-05-16, SHA `4f22223`** — Initial budget substrate. Schema v4 migration. Strictly PPC. Theme + overall + asin scopes. Status ladder (no_budget / no_spend / under / at / over). LATERAL theme attribution. Content-change markers. Four HTTP endpoints + two live smoke checks. 64 QA assertions.
