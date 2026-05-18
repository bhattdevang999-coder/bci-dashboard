# CITATION_CHAIN.md — 5-Layer Reasoning on Every NIS Output

**Status:** design (pre-build)
**Author:** Devang / Atlas
**Date:** 2026-05-18
**Milestone:** M3 (Days 1–3, paired with M1)

---

## Purpose

Every piece of content NIS generates ships with an explicit citation
chain identifying which substrate rows drove which choices. The
operator can expand any citation to verify the reasoning, reject any
citation (triggers regeneration), or accept the output.

This is the architectural commitment that makes Atlas auditable
rather than just generative.

---

## The 5 layers cited at content-gen time

These are layers 1–5 of L0. Layers 6–9 (market, competitor, unit
economics, goals) become citable when their substrates populate in
later phases.

```
LAYER          CITATION ANCHOR        EXAMPLE CITATION
─────────────────────────────────────────────────────────────────
factual        asin_metadata row     "7/8 length" → asin_metadata#247
strategic      operator_position or  "Athletic only, no Casual" →
               brand_position row     operator_position#88
voice          brand_profile@version  "buttery-soft" →
                                     brand_voice@v1.4
evidence       outcome_event or       "High-Rise prefix lifted CVR
               substrate_event row    +8% on B0NOV2" →
                                     outcome_event#5519
calibrated_ext recommendation_ingest  "Yoga Leggings prefix
               + calibration_state    rejected, agency cal weight
                                     0.38" → rec#142
```

A sixth flag exists outside the layer list:

```
convention   no substrate row       "title starts with brand name
                                     'Novelle Velune'" → convention
```

`convention` is for choices the LLM made on Amazon listing
convention or general-knowledge basis. Convention flags are SHOWN
to operator (per operator decision: "show convention flags = true"),
not hidden. Operator can either accept the convention silently or
promote it to an `operator_position` if they want it formally
captured.

---

## The NIS prompt (title-generation variant)

This is the prompt template. Same shape for bullet, description,
A+ content with section-specific constraints. Substituted at
runtime with substrate values from L0.

```
You are generating an Amazon listing TITLE for a specific ASIN.

═════════════════════════════════════════════════════════════════
CONTEXT
═════════════════════════════════════════════════════════════════

[L0 bundle rendered as structured sections — Layers 1-5 active,
 Layers 6-9 may be empty in early phases]

═════════════════════════════════════════════════════════════════
UNKNOWNS BLOCKING FULL CONFIDENCE
═════════════════════════════════════════════════════════════════

[N open unknowns rendered with evidence_path]

═════════════════════════════════════════════════════════════════
YOUR JOB
═════════════════════════════════════════════════════════════════

Generate ONE primary title and TWO alternates.

CONSTRAINTS:
  - Amazon title char limit 200; target 150-180
  - Must include facts from Layer 1
  - Must NOT include any banned phrase from Layer 3
  - Must align with positioning from Layer 2
  - Should reference (with citation) proven patterns from Layer 4
  - May incorporate Layer 5 only if calibration_weight > 0.5
    OR operator has explicitly accepted the rec
  - Include `convention` flags when you must use a phrase that
    has no substrate basis

CITATION REQUIREMENTS:
  Every word/phrase choice must cite the substrate row(s) that
  drove it. Valid layers: factual | strategic | voice | evidence
                         | calibrated_external | convention.

CONFIDENCE BREAKDOWN (self-reported, 0.0-1.0 each):
  - voice_compliance
  - factual_accuracy
  - positioning_match
  - evidence_grounding
  - convention_share (lower is better — more substrate-grounded)

═════════════════════════════════════════════════════════════════
OUTPUT FORMAT (strict JSON)
═════════════════════════════════════════════════════════════════
{
  "primary_title": "...",
  "alternates": ["...", "..."],
  "citations": [
    {
      "claim": "<what choice this citation supports>",
      "layer": "factual|strategic|voice|evidence|calibrated_external|convention",
      "source_row_ids": ["<id>"],
      "rationale": "<one sentence>"
    }
  ],
  "confidence_self_reported": 0.0,
  "confidence_breakdown": {...},
  "open_unknowns_referenced": [<unknown_id>],
  "convention_flags": [
    {"claim": "...", "rationale": "..."}
  ]
}
```

---

## Citation verifier

Post-generation, before showing operator, every cited `source_row_id`
is verified:

1. **Exists check**: row ID exists in the named substrate table.
2. **Content match check**: the LLM's `rationale` is not contradicted
   by the actual row content. (Implemented as a second LLM call:
   "Does this row support this claim? yes/no/partial.")

Failures:
- Row doesn't exist → citation is replaced with `{"layer":
  "convention", "rationale": "verifier: cited row not found"}`,
  and a flag is raised to operator.
- Content mismatch → citation kept but tagged with
  `verifier_status: "weak"` and shown in yellow in the UI.

This is the guardrail against citation hallucination. Without it,
the entire citation system is theater.

---

## Schema additions (M3)

```sql
-- Add citation storage to substrate_events
ALTER TABLE substrate_events
  ADD COLUMN IF NOT EXISTS citations          JSONB,
  ADD COLUMN IF NOT EXISTS citation_outcomes  JSONB,
  ADD COLUMN IF NOT EXISTS confidence_breakdown JSONB,
  ADD COLUMN IF NOT EXISTS convention_flags   JSONB;

-- Citation rejections — operator-driven, future calibration input
CREATE TABLE IF NOT EXISTS citation_rejections (
  rejection_id      TEXT PRIMARY KEY,
  decision_event_id TEXT NOT NULL,
  citation_layer    TEXT NOT NULL,
  citation_source_id TEXT NOT NULL,
  reason            TEXT,
  rejected_by       TEXT,
  rejected_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_citation_rejections_layer
  ON citation_rejections (citation_layer, rejected_at DESC);
```

---

## UI surface

Every NIS generation renders:

```
GENERATED TITLE (confidence: 64%)

  <title text>

  Voice compliance:    [▓▓▓▓▓▓▓▓▓░] 95%
  Factual accuracy:    [▓▓▓▓▓▓▓▓▓▓] 99%
  Positioning match:   [▓▓▓▓▓▓▓▓░░] 80%
  Evidence grounding:  [▓▓▓▓░░░░░░] 45% ← held by unknowns
  Convention share:    [▓░░░░░░░░░] 15%

  ▼ Reasoning chain (5 citations + 1 convention)
    [factual]    "7/8" + "Hidden Waistband Pocket" + "High-Rise"
                 → asin_metadata#247
    [voice]      "Buttery-Soft"
                 → brand_voice@v1.4
    [strategic]  "Athletic Fit" — no Casual/daywear
                 → operator_position#88
    [evidence]   "High-Rise" prefix
                 → outcome#5519 (B0NOV2, +8% CVR, n=1)
    [calibrated_external]
                 "Yoga Leggings" prefix rejected
                 → rec#142 (agency cal weight 0.38)
    [convention] "Novelle Velune" brand prefix
                 → no substrate; standard Amazon practice
                 [ Save as operator_position ]

  ▼ 6 open unknowns affecting confidence
    [factory] GSM, exact pocket dimensions, UPF
    [agency]  B3 Part Number rationale, B4 Lifestyle
    [outcome] Premium-tier keyword bid elasticity

  ▼ 2 alternates
    1. ...
    2. ...

  [ Accept primary ] [ Edit ] [ Regenerate ]
  [ Pick alt 1 ]    [ Pick alt 2 ]
```

Hover/click on any citation expands to show:
- The actual substrate row content at this moment
- The row's last-modified timestamp
- A [Reject this citation and regenerate] button

---

## Operator actions and what they write

```
ACTION                    WRITES TO SUBSTRATE
─────────────────────────────────────────────────────────────
Accept primary as-is       substrate_events.status='accepted'
                           citation_outcomes records all
                             citations as 'accepted'

Edit text directly         substrate_events.accepted_value =
                             <edited text>
                           character-level diff captured
                           citation_outcomes per citation:
                             'preserved' | 'modified' | 'removed'

Reject a citation          citation_rejections row inserted
                           regeneration triggered without that
                             row in context
                           future similar generations weight
                             that row lower for this decision_class

Save convention as         operator_positions row inserted with
operator_position           the convention promoted to a rule
                           future generations cite from
                             operator_position instead of flagging
                             as convention

Pick alternate              substrate_events.accepted_value =
                             <chosen alternate>
                           same citation tracking as primary
```

---

## Failure modes

1. **Citation hallucination escapes verifier** — verifier is itself
   an LLM. It can rubber-stamp incorrect citations. Mitigation:
   sample-based human review during first 30 days, plus calibration
   on verifier accuracy.

2. **Operator override fatigue** — if every generation has 5+
   convention flags, operator stops reviewing them. Mitigation:
   ship with conventions hidden by default? — REJECTED, operator
   chose to show. Real mitigation: convention rate decreases as
   operator_positions accumulate.

3. **Confidence theater** — confidence_self_reported is just an
   LLM's guess. It is not calibrated to actual outcome accuracy
   in the first 90 days. Display it but flag it as "self-reported,
   not yet calibrated against outcomes" until calibration_state
   has signal.

---

## What this does NOT do (M3 scope guard)

- Does NOT calibrate confidence against outcomes. That requires
  outcome data + the calibration_state writer (later milestone).
- Does NOT auto-update prior generations when an unknown closes.
  That's a benchmark-review feature in M5.
- Does NOT propagate citation rejection across decision_classes.
  Rejection scoped to the (citation_layer, source_id, decision_class)
  triple.

---

## Version history

Append below this line.

- **v1.0 — 2026-05-18, present commit** — Initial design. 5-layer
  citation chain on NIS specified. Citation verifier required.
  Convention flag shown by default per operator decision. Citation
  rejection writes to substrate; regeneration without rejected
  citation. Schema additions: citations, citation_outcomes,
  confidence_breakdown, convention_flags on substrate_events;
  new citation_rejections table.
