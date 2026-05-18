# UNKNOWNS.md — Ignorance Catalog Primitive

**Status:** design (pre-build)
**Author:** Devang / Atlas
**Date:** 2026-05-18
**Milestone:** M3 (Days 1–3) for table + emit hooks; queue UI M4+

---

## Purpose

The dashboard's first job is to know what it doesn't know. Every
reasoning chain that hits a missing input emits an `unknowns` row.
The catalog of open unknowns becomes operator-actionable: routed
to the right owner (factory, agency, operator, test, outcome),
prioritized by what they're blocking, closeable as evidence
arrives.

This is the substrate equivalent of the principle the operator
named explicitly: "the system should be designed to ask more,
fine-tune, calibrate, and eventually arrive at smart decisions."

Without unknowns as first-class data, the system fills gaps with
synthetic confidence. With unknowns, gaps are visible, addressable,
and time-trackable.

---

## Schema

```sql
CREATE TABLE IF NOT EXISTS unknowns (
  unknown_id           TEXT PRIMARY KEY,
  workspace_id         TEXT NOT NULL,

  scope                TEXT NOT NULL,
                       -- 'global' | 'brand' | 'asin' | 'family'
                       -- | 'decision_class'
  scope_ref            TEXT,
                       -- ASIN | family_id | decision_class name
                       -- nullable when scope='global'

  question             TEXT NOT NULL,
                       -- the actual question being asked

  required_for         TEXT[] NOT NULL,
                       -- decision_class names that this unknown
                       -- affects

  evidence_path        TEXT NOT NULL,
                       -- 'factory_spec_sheet'
                       -- 'agency_response'
                       -- 'helium10_weekly'
                       -- 'a_b_test'
                       -- 'outcome_measurement'
                       -- 'operator_decision'
                       -- 'declared_unknowable'

  status               TEXT NOT NULL DEFAULT 'open',
                       -- 'open' | 'partial' | 'answered'
                       -- | 'declared_unknowable' | 'expired'

  priority             TEXT DEFAULT 'normal',
                       -- 'launch_blocking' | 'high' | 'normal' | 'low'

  partial_evidence     JSONB DEFAULT '[]'::jsonb,
                       -- accumulating evidence before answered

  answer_value         JSONB,
                       -- structured answer when answered
  answer_source        TEXT,
                       -- where the answer came from
  answered_at          TIMESTAMPTZ,
  answered_by          TEXT,

  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_by_event_id  TEXT,
                       -- which decision surfaced this unknown
  created_by_module    TEXT,
                       -- 'nis' | 'pricing' | 'rec_evaluator' | etc.

  meta                 JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_unknowns_status_priority
  ON unknowns (workspace_id, status, priority,
               created_at DESC);

CREATE INDEX IF NOT EXISTS idx_unknowns_evidence_path
  ON unknowns (workspace_id, status, evidence_path);

CREATE INDEX IF NOT EXISTS idx_unknowns_scope_ref
  ON unknowns (workspace_id, scope, scope_ref)
  WHERE status IN ('open', 'partial');
```

---

## Emit hook (where unknowns get created)

Every module that calls `build_context()` runs a completeness
check against a `decision_class_requirements` config. Missing
required-OR-nice-to-have inputs trigger an unknown emit:

```python
def emit_unknown(
    workspace_id: str,
    scope: str,
    scope_ref: Optional[str],
    question: str,
    required_for: list[str],
    evidence_path: str,
    *,
    priority: str = 'normal',
    created_by_event_id: Optional[str] = None,
    created_by_module: Optional[str] = None,
) -> str:  # returns unknown_id
    ...
```

Deduplication: if an open unknown with the same `(workspace_id,
scope, scope_ref, question, evidence_path)` already exists, the
new emit appends `required_for` and `created_by_event_id` to the
existing row instead of creating a duplicate. This is how multiple
decisions can collectively block on one unknown.

---

## Decision_class_requirements config

A separate config table (or JSON config file in repo) declares
what context is needed per decision_class. Editing this is a
config change, not code.

```yaml
# substrate/decision_class_requirements.yml
title_generation:
  required:
    - asin_metadata.material
    - asin_metadata.length
    - asin_metadata.fit_type
    - asin_metadata.pocket_presence
    - asin_metadata.size
    - asin_metadata.color
    - brand_position
    - brand_voice
  nice_to_have:
    - asin_metadata.fabric_gsm
    - asin_metadata.upf_rating
    - outcome_events.cohort_evidence
    - competitor_state.title_patterns
    - calibration_state.agency_title_recs
  penalty_per_missing_required: 0.25
  penalty_per_missing_nice_to_have: 0.05

pricing_decision_mode1:
  required:
    - cost_inputs.landed_cost
    - cost_inputs.fba_fee
    - cost_inputs.referral_pct
    - pricing_logic.floor_rule
    - pricing_logic.ceiling_rule
    - brand_position
  nice_to_have:
    - competitor_state.ceiling_anchor_value
    - outcome_events.elasticity_data
    - asin_state.bsr_current
  penalty_per_missing_required: 0.20
  penalty_per_missing_nice_to_have: 0.05

# ... per decision_class
```

These weights tune to feel right over the first 30 days.
Starting values are conservative.

---

## Status lifecycle

```
       created
          ↓
       [ open ]
          ↓
  ┌──── partial ────┐
  │                  │
  ↓                  ↓
[ answered ]   [ declared_unknowable ]
                      │
                      ↓
              still affects confidence?
                      ↓
                     no
                      ↓
              [ expired / archived ]
```

- **open** → no evidence yet
- **partial** → some evidence exists but answer is incomplete
  (e.g., factory provided fabric weight but not UPF)
- **answered** → resolved with a substrate-stored answer; confidence
  penalty removed for affected decisions
- **declared_unknowable** → operator explicitly accepts this gap
  is irreducible; confidence penalty removed; system stops asking
- **expired** → answered long ago and the answer is now stale
  (e.g., competitor price from 6 months ago); auto-set by aging
  rules per evidence_path

---

## Owner routing

`evidence_path` maps to an owner queue. The dashboard has one
unknowns view filterable by queue:

```
EVIDENCE PATH              OWNER          QUEUE
─────────────────────────────────────────────────────────
factory_spec_sheet          manufacturer   Factory Questions
agency_response             agency         Agency Pending
helium10_weekly             data_feed      External Data
operator_decision           operator       Your Decisions
a_b_test                    time           Tests Running
outcome_measurement         time           Awaiting Outcomes
declared_unknowable         none           Archived
```

The Factory Questions queue can be exported as a single doc
to forward to the factory. The Agency Pending queue feeds the
tokenized response link (see RECOMMENDATION_INGEST.md). The
operator-decision queue is reviewed in the daily/weekly
operator cadence.

---

## When unknowns close

When a substrate write resolves an unknown:

```python
def resolve_unknown(
    unknown_id: str,
    answer_value: dict,
    answer_source: str,
    answered_by: str,
) -> None:
    # 1. Mark unknown as answered
    # 2. Propagate the answer to the source table
    #    (e.g., asin_metadata if it's a factory fact)
    # 3. Flag any content_benchmarks that referenced this unknown
    #    for operator review (do NOT auto-regenerate — that
    #    rewrites history)
    # 4. Bump confidence on any open decision_events that were
    #    waiting on this unknown
    ...
```

The propagation step (#2) is important: an unknown's answer is
not just stored in `unknowns.answer_value`. It's written through
to the canonical substrate table (e.g., `asin_metadata`,
`cost_inputs`, `competitor_state`). The `unknowns` row remains
as the audit trail of when the gap closed and what source filled
it.

---

## UI — the Unknowns dashboard

```
UNKNOWNS — Novelle

  By queue                          Open   Age   Blocking
  ──────────────────────────────────────────────────────
  Factory Questions                  14   2.3d   all NIS
  Agency Pending                      6     0d   titles
  Your Decisions                      3   0.5d   pricing
  Tests Running                       5   12d    voice opt
  Awaiting Outcomes                  11   3-30d  attribution
  Archived (unknowable)               2     —    —

  By scope
  ──────────────────────────────────────────────────────
  Velune pocket family (parent)      8
  Velune no-pocket family (parent)   7
  Velune children                   12 across 40 ASINs
  Global (brand-level)                4

  By priority
  ──────────────────────────────────────────────────────
  Launch-blocking                     4   ← review now
  High                                7
  Normal                             19
  Low                                 9

  [ Export Factory Questions doc ]
  [ Generate Agency response link ]
  [ Resolve unknown manually ]
  [ Declare unknowable ]
```

---

## What this does NOT do (scope guard)

- Does NOT predict which unknowns will be most valuable to close.
  That's a future calibration feature ("closing this unknown
  historically lifted confidence by X% on average").
- Does NOT auto-close stale unknowns. Operator must declare them
  unknowable or answered explicitly. (Aging-to-expired only
  applies to answered-but-stale, not open.)
- Does NOT cap the number of unknowns. If the table gets noisy,
  that's a substrate signal — the dashboard is being honest about
  how much it doesn't know.

---

## Failure modes

1. **Operator overwhelm**: 500+ open unknowns by end of week 1
   is plausible. UI defaults to grouped views and priority
   sorting. Operator must learn to use declared_unknowable
   liberally to keep the queue tractable.

2. **Duplicate emits across decision_classes**: dedupe key is
   `(scope, scope_ref, question, evidence_path)`. Same question
   from different decisions appends `required_for`, doesn't
   duplicate.

3. **Stale answered unknowns**: e.g., a competitor price answered
   3 months ago is no longer current. Aging rules per
   evidence_path mark these as expired and re-open them as
   new unknowns.

---

## Version history

Append below this line.

- **v1.0 — 2026-05-18, present commit** — Initial design.
  `unknowns` table specified. Emit hook contract defined.
  Owner-routed queues. Decision_class_requirements config-driven.
  Status lifecycle: open → partial → answered/unknowable/expired.
  Resolution propagates to canonical substrate tables, not just
  the unknowns row. Benchmark review (not auto-regenerate) on
  resolution.
