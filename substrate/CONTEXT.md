# CONTEXT.md — L0 Context Injection Layer

**Status:** design (pre-build)
**Author:** Devang / Atlas
**Date:** 2026-05-18
**Milestone:** M1 (Days 1–3)

---

## Purpose

A single substrate module — `substrate/context.py` — that every LLM
call in Atlas routes through to assemble the relevant substrate state
into a structured context object. Today, every module (NIS, Brand
Voice, Variations, Image NIS, Budget) hand-rolls its own context
loading. This is inconsistent, partially redundant, and the reason
Atlas's reasoning can't compose across decision classes.

L0 is the single chokepoint that fixes this. Every future module
that calls an LLM goes through `build_context()`. Every existing
module is retrofitted to use it.

---

## Contract

```python
def build_context(
    workspace_id: str,
    asin: Optional[str],
    decision_class: str,
    *,
    operator_id: Optional[str] = None,
    include_unknowns: bool = True,
    override_layers: Optional[set[str]] = None,
) -> ContextBundle
```

`ContextBundle` is a structured dict (typed alias) carrying the
nine reasoning layers, the open unknowns affecting this decision,
and a list of substrate row IDs consulted (for provenance logging).

### Decision classes (initial set)

These match the `calibration_class` taxonomy. Adding a new decision
class is a config edit, not a code change.

```
content_generation:
  - title_generation
  - bullet_generation
  - description_generation
  - a_plus_content
  - image_brief
  - backend_field_fill

evaluation:
  - recommendation_evaluation
  - voice_compliance_check
  - variation_parentage

pricing:
  - pricing_decision_mode1   (LLM market reasoning)
  - pricing_decision_mode2   (calibrated, month 6+)

other:
  - cost_review
  - launch_brief
```

---

## Layers assembled

L0 reads from substrate tables and assembles a structured object.
Each layer is a top-level key in the returned bundle. Empty layers
are present but empty — never omitted — so downstream consumers
can assume a stable schema.

```
LAYER 1 — factual
  Source:  asin_metadata (parent inheritance applied)
  Content: physical product facts, immutable spec

LAYER 2 — strategic
  Source:  operator_positions (scope: global, asin, family,
                                decision_class),
           brand_position,
           goals

LAYER 3 — voice
  Source:  brand_profile (latest profile_version)
  Content: tone descriptors, hero adjectives, banned phrases,
           like/unlike examples

LAYER 4 — evidence
  Source:  outcome_events (filtered to cohort of similar ASINs,
                            metric set relevant to decision_class,
                            past 60–90 days)
           substrate_events (prior decisions of same class)
  Content: prior outcomes, cohort-adjusted lift estimates,
           operator's accept/edit history for similar decisions

LAYER 5 — calibrated_external
  Source:  recommendation_ingest WHERE scope_asins ∋ asin
                                  AND status NOT IN ('resolved',
                                                     'rejected'),
           calibration_state WHERE source matches rec.source
                              AND class matches decision_class
  Content: external recommendations affecting this decision,
           with each weighted by the source's calibration on
           this decision class

LAYER 6 — market_state    (placeholder until Phase 4)
  Source:  asin_state (when populated)
  Content: BSR, keyword rank, search position — empty for now

LAYER 7 — competitor_state (placeholder until Phase 4)
  Source:  competitor_state (when populated)
  Content: tracked competitor prices, listing changes — empty
           for now except for operator-typed CRZ ceiling reference

LAYER 8 — unit_economics
  Source:  cost_inputs, brand_overhead, margin rollup from
           substrate/margin.py
  Content: per-unit costs, current margin math, floor/ceiling
           per pricing_logic rule

LAYER 9 — goals
  Source:  goals table (placeholder until Phase 3)
  Content: launch vs steady-state regime, active KPI targets

UNKNOWNS
  Source:  unknowns WHERE status IN ('open', 'partial')
                     AND scope OR scope_ref applies
  Content: every gap relevant to this decision class
```

---

## Provenance contract

When a decision_class consumer calls `build_context()`, two
things happen:

1. The returned bundle carries `context_rows_read: list[str]` —
   every substrate row ID consulted (even if not used downstream).
2. When the consumer commits a decision_event, it writes back
   `context_rows_used: list[str]` (subset actually cited by LLM
   output), plus `evidence_strength` and `calibration_class`.

These four fields (`context_rows_read`, `context_rows_used`,
`evidence_strength`, `calibration_class`) are added to
`substrate_events` as part of M1 schema migration v6.

---

## Schema migration v6

```sql
ALTER TABLE substrate_events
  ADD COLUMN IF NOT EXISTS context_rows_read   TEXT[],
  ADD COLUMN IF NOT EXISTS context_rows_used   TEXT[],
  ADD COLUMN IF NOT EXISTS evidence_strength   TEXT,
  ADD COLUMN IF NOT EXISTS calibration_class   TEXT;

CREATE INDEX IF NOT EXISTS idx_substrate_events_calibration_class
  ON substrate_events (workspace_id, calibration_class, occurred_at DESC);

INSERT INTO substrate_schema_version (version, notes)
  VALUES ('v6', 'L0 context injection + decision provenance.')
  ON CONFLICT (version) DO NOTHING;
```

---

## Retrofit scope (M1)

The following modules get their `build_context` retrofit in M1.
NIS retrofit specifically lands in M1+M3 paired build.

```
substrate/nis.py            — replace hand-rolled context loading
substrate/brand_voice.py    — read_voice already supports versioning;
                              add build_context wrapper for any LLM
                              prompts using voice
substrate/variations.py     — retrofit /api/merge/analyze
substrate/budget.py         — retrofit /api/budget/* LLM calls
                              (lower priority)
substrate/image_nis.py      — retrofit /api/beta-image-nis/generate
                              (lower priority)
```

**Retrofit order in the sprint:**
- Day 1: build_context() core implementation + QA suite
- Day 2: NIS retrofit (highest-traffic, most-tested module)
- Day 3: ship M1+M3 jointly so citation chain UI is testable

Other modules retrofit during M2-M5 in their respective days.

---

## QA contract

`qa_context_layer.py` asserts:

1. `build_context()` returns a bundle with all 9 layers populated
   (some may be empty objects, but the keys exist).
2. For an ASIN with known metadata, `factual` layer contains
   expected fields.
3. For a workspace with known voice version, `voice` layer
   contains tone_descriptors and banned_phrases.
4. For a workspace with cost_inputs, `unit_economics` layer
   computes contribution margin per unit_economics/margin.py.
5. For an ASIN with prior outcome_events, `evidence` layer is
   non-empty and contains rows tagged with cohort information.
6. Provenance columns added by migration v6 exist and accept
   inserts.
7. Calling `build_context()` with override_layers={'evidence'}
   skips that layer (operator override path).

---

## What this does NOT do (M1 scope guard)

- Does NOT generate any LLM output. Pure context assembly.
- Does NOT update calibration_state. That happens in calibration
  writers triggered by outcome arrivals (later milestones).
- Does NOT enforce decision_class_requirements. That's M3.
- Does NOT verify citation row IDs. That's M3.
- Does NOT cache context. Each call reads fresh from substrate
  to avoid stale reads. Cache layer is a future optimization.

---

## Failure modes

1. **Substrate unavailable** — `build_context()` returns a bundle
   with all empty layers + `evidence_strength='absent'`. LLM
   callers must handle empty context gracefully and emit an
   unknown row.

2. **Token bloat** — assembled context can exceed LLM context
   window for ASINs with deep history. Each layer has a
   row-count cap (e.g., evidence layer max 50 most recent rows).
   Truncation is deterministic (most recent first) and logged.

3. **Schema drift** — new substrate tables added later need
   explicit layer-assignment. The default assumption is the
   bundle does NOT auto-detect new tables. Adding a table to a
   layer is a code change.

---

## Version history

Append below this line. Do not edit prior entries.

- **v1.0 — 2026-05-18, present commit** — Initial design. L0
  context injection layer specified. 9-layer schema locked.
  Provenance columns added to substrate_events. Retrofit scope:
  NIS first (M1+M3 paired), other modules during M2-M5.
  Decision_class taxonomy seeded with 13 classes; additions are
  config edits.
