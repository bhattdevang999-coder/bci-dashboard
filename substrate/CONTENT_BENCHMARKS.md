# CONTENT_BENCHMARKS.md — Reusable Pattern Substrate

**Status:** design (pre-build)
**Author:** Devang / Atlas
**Date:** 2026-05-18
**Milestone:** M5 (Days 9–10)

---

## Purpose

When the operator approves a piece of generated content along
with its full reasoning chain, that approval becomes a benchmark.
Future generations on similar ASINs seed from the benchmark
instead of re-deriving from scratch. This is what makes
40-ASIN families operationally tractable: the parent benchmark
gets resolved once, the 19 sibling variants adapt only the
variation-axis deltas.

This is also how the system carries forward what worked. The
benchmark captures the full context at lock time, the citations
that supported it, the unknowns it was created with. When an
unknown later closes, the benchmark is flagged for operator
review (not auto-regenerated — that would rewrite history).

---

## Schema

```sql
CREATE TABLE IF NOT EXISTS content_benchmarks (
  benchmark_id          TEXT PRIMARY KEY,
  workspace_id          TEXT NOT NULL,

  scope                 TEXT NOT NULL,
                        -- 'global' | 'family' | 'asin'
                        -- | 'family_decision_class'
  scope_ref             TEXT,
                        -- e.g., 'velune_pocket_family'

  benchmark_type        TEXT NOT NULL,
                        -- 'title' | 'bullets' | 'description'
                        -- | 'a_plus' | 'image_brief'
                        -- | 'backend_fields' | 'launch_brief'

  approved_value        JSONB NOT NULL,
                        -- the actual content (text, structured
                        --  fields, etc.)

  resolved_inputs       JSONB NOT NULL,
                        -- the full context bundle at lock time
                        -- (snapshot for reproducibility)

  source_event_id       TEXT NOT NULL,
                        -- which substrate_event this came from

  citations             JSONB NOT NULL,
                        -- the full citation chain from the
                        -- approving decision

  open_unknowns_at_approval TEXT[],
                        -- unknown_ids that were open when this
                        -- benchmark was locked

  status                TEXT NOT NULL DEFAULT 'active',
                        -- 'active' | 'review_recommended'
                        -- | 'superseded' | 'archived'

  review_reason         TEXT,
                        -- when status='review_recommended'

  superseded_by         TEXT,
                        -- benchmark_id of replacement

  approved_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  approved_by           TEXT NOT NULL,

  last_used_at          TIMESTAMPTZ,
                        -- bumped when a new generation seeds
                        -- from this benchmark
  used_count            INTEGER DEFAULT 0,

  meta                  JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_benchmarks_scope
  ON content_benchmarks (workspace_id, scope, scope_ref, status);

CREATE INDEX IF NOT EXISTS idx_benchmarks_type
  ON content_benchmarks (workspace_id, benchmark_type, status,
                          approved_at DESC);
```

---

## Lock-in flow

When the operator clicks "Save as benchmark" on an approved
NIS output:

```
SAVE AS BENCHMARK

  Content type:    title
  Approved value:  "Novelle Velune High-Rise 7/8 Leggings
                    — Buttery-Soft Hidden Waistband Pocket,
                    Athletic Fit"

  Scope:           Choose how broadly this benchmark applies.
                   ☐ This ASIN only (B0VEL-PKT-BLK-M)
                   ☐ Family: velune_pocket  ← default
                   ☐ Global (all titles)

  Citations from approving decision (5):
    [factual]    "7/8" + "Hidden Waistband Pocket" + "High-Rise"
                 → asin_metadata#247
    [voice]      "Buttery-Soft" → brand_voice@v1.4
    [strategic]  "Athletic Fit" → operator_position#88
    [evidence]   "High-Rise" → outcome#5519
    [calibrated_external] agency "Yoga Leggings" rejected
                 → rec#142

  Open unknowns at this lock-in (6):
    [factory] fabric_gsm, UPF, pocket dimensions
    [agency]  Part Number rationale (rec#142 B3),
              Lifestyle (rec#142 B4)
    [outcome] premium-tier keyword bid elasticity

  Future generations on family:velune_pocket children will
  seed from this benchmark. They will adapt only:
    - color_name
    - size
    - any unknown-resolution that affects this family

  [ Save benchmark ]   [ Cancel ]
```

Operator confirms scope. Row gets written. Status=active.

---

## Benchmark-seeded generation

When NIS is asked to generate a title for B0VEL-PKT-CHA-L
(a sibling in the same family):

```
Generation flow:
  1. build_context() runs as normal
  2. context.benchmarks_applicable() returns
     content_benchmarks WHERE
       scope IN ('global', 'family', 'asin')
       AND scope_ref matches asin / family / global
       AND benchmark_type = 'title'
       AND status = 'active'
  3. If matching benchmark found:
       - Prompt template switches to "seeded" variant
       - Benchmark approved_value + citations injected
       - LLM instruction: "Adapt this benchmark for the new
         color/size; preserve all citation-grounded structure;
         change only variation-axis-specific elements"
       - Generated output cites the benchmark itself as a
         citation source ([benchmark] benchmarks#71)
  4. If no benchmark: normal generation flow
```

Result: titles for the 19 sibling pocket-family ASINs adapt
the parent benchmark in ~1 minute each, with consistent
positioning, voice, and structural choices.

---

## Auto-flag on unknown resolution

When an unknown that this benchmark was created with later
closes:

```
Unknown #142 resolved (fabric GSM = 160 g/m²)
  Source: factory_response_2026_05_24

  Benchmarks affected:
    benchmarks#71  (velune_pocket_family title v1)
      ← created with this unknown open
      ← status flipped to 'review_recommended'
      ← review_reason: "fabric_gsm now known (160 g/m²);
                        consider whether 'lightweight' /
                        'supportive' framing would improve"

    benchmarks#84  (velune_pocket_family bullets v1)
      ← same flip

Operator dashboard surfaces:
  ⚠ 2 benchmarks have closed unknowns and may need review
    velune_pocket_family.title (v1, approved May 21)
      → fabric_gsm now known
    velune_pocket_family.bullets (v1, approved May 21)
      → fabric_gsm now known

  [ Review and re-approve / archive / regenerate ]
```

The system does NOT auto-regenerate. Operator decides:
- Keep current benchmark (status flips back to 'active')
- Regenerate with new substrate evidence (creates new
  benchmark v2, supersedes v1)
- Archive entirely (status='archived')

---

## Benchmark hierarchy (scope conflicts)

When multiple benchmarks could apply, more-specific wins:

```
scope=asin         (most specific, applies first)
   ↓
scope=family
   ↓
scope=global       (most general, applies if nothing else)
```

If both an asin-scoped and family-scoped benchmark exist for
the same benchmark_type, the asin-scoped one is used.
Operator can see all applicable benchmarks in the ASIN Brain
page and override which to seed from per generation.

---

## What this does NOT do (scope guard)

- Does NOT auto-regenerate benchmarks when unknowns close.
  Operator review required.
- Does NOT propagate benchmark deletions retroactively.
  Old decisions that cited the benchmark remain intact;
  citation says "benchmark archived after this decision."
- Does NOT support cross-brand benchmarks (Velune benchmark
  doesn't seed Brand-2 generations). Workspace-scoped only.
- Does NOT version benchmark contents automatically when
  source substrate changes (e.g., brand_voice bumps to v1.5).
  Benchmarks freeze the substrate state at lock time.
  Operator reviews when relevant substrate changes.

---

## Failure modes

1. **Benchmark drift**: brand_voice bumps to v1.5 but
   benchmarks created under v1.4 keep using v1.4 patterns.
   Detection: when voice version bumps, surface affected
   benchmarks for review. Same flow as unknown resolution.

2. **Over-benchmark**: operator locks too many benchmarks
   early; they become inconsistent with each other. Cap on
   active benchmarks per (scope, benchmark_type) — default
   3 — with operator forced to archive before adding a 4th.

3. **Benchmark applied to wrong ASIN**: family scope is
   matched against asin's variation_family in
   asin_metadata. If an ASIN's family is mistagged,
   wrong benchmark applies. Mitigation: asin_metadata
   confirmation flow at onboarding catches family tagging.

---

## Version history

Append below this line.

- **v1.0 — 2026-05-18, present commit** — Initial design.
  content_benchmarks table specified. Lock-in flow on
  operator approve. Scope hierarchy (asin > family > global).
  Benchmark-seeded generation for sibling ASINs. Auto-flag
  on unknown resolution (review recommended, not regenerate).
  Operator decides supersede / archive / keep. Voice-version
  bumps flag affected benchmarks via same mechanism.
