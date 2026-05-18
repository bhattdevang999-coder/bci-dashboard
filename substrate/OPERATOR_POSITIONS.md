# OPERATOR_POSITIONS.md — Operator Logic as First-Class Substrate

**Status:** design (pre-build)
**Author:** Devang / Atlas
**Date:** 2026-05-18
**Milestone:** M2 (Days 4–5)

---

## Purpose

The operator's logic — their beliefs, preferences, style rules,
strategic decisions, hard refusals — stored as substrate, not
as chat. Every decision Atlas evaluates reads from operator
positions in Layer 2. Every operator edit can be promoted to a
position so future similar decisions honor it automatically.

Without this, operator logic dies at the end of each chat session.
With this, the dashboard accumulates operator intuition the same
way it accumulates outcomes.

---

## Scope contract: ONE operator per brand

Per the operator's lock-in earlier today: Novelle has one operator
(Devang). The `operator_id` field exists in the schema for future
multi-brand portability but is functionally constant per workspace.

Multi-operator support is explicitly OUT OF SCOPE for the
foreseeable build. When (and if) a brand needs multi-operator,
that's a future architectural decision; the schema accommodates
it without requiring re-design today.

---

## Schema

```sql
CREATE TABLE IF NOT EXISTS operator_positions (
  position_id        TEXT PRIMARY KEY,
  workspace_id       TEXT NOT NULL,
  operator_id        TEXT NOT NULL DEFAULT 'devang',

  scope              TEXT NOT NULL,
                     -- 'global' | 'brand' | 'asin' | 'family'
                     -- | 'decision_class' | 'family_decision_class'
  scope_ref          TEXT,
                     -- ASIN | family_id | decision_class | combined

  claim              TEXT NOT NULL,
                     -- the operator's belief or rule
                     -- e.g., "Athletic only, no Casual"
                     -- e.g., "Pocket family titles must include
                     --        'pockets' in first 5 words"
                     -- e.g., "Em-dash preferred over comma in
                     --        title second clauses"

  reasoning          TEXT,
                     -- why this position exists
                     -- e.g., "Brand position is premium-adjacent;
                     --        Casual register dilutes intent"

  position_type      TEXT NOT NULL DEFAULT 'strategic',
                     -- 'strategic' | 'style' | 'hard_refusal'
                     -- | 'workflow' | 'preference'

  status             TEXT NOT NULL DEFAULT 'active',
                     -- 'active' | 'archived' | 'superseded'

  superseded_by      TEXT,  -- position_id of superseding row
  evidence_refs      TEXT[],
                     -- substrate row IDs supporting the position
                     -- e.g., outcome events, prior decisions

  revision           INTEGER NOT NULL DEFAULT 1,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_by_event_id TEXT,
                     -- which decision/edit surfaced this position
  last_reviewed_at   TIMESTAMPTZ,
                     -- bumped when operator reviews and reaffirms

  meta               JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_op_positions_scope
  ON operator_positions (workspace_id, status, scope, scope_ref);

CREATE INDEX IF NOT EXISTS idx_op_positions_type
  ON operator_positions (workspace_id, status, position_type);
```

---

## Position types

```
TYPE              EXAMPLES                              ALTERS
─────────────────────────────────────────────────────────────────
strategic         "Athletic only, no Casual"            Layer 2
                  "Premium positioning"                 reasoning
                  "No discount language"

style             "Em-dash preferred over comma"        NIS output
                  "Hero adjective in first 5 words"     structure
                  "Avoid superlatives"

hard_refusal      "Never use word 'shapewear'"          NIS constraint
                  "Refuse Lifestyle = Casual"           NIS constraint
                  "Reject any agency rec mentioning
                   'magic' or 'miracle'"

workflow          "Require operator review before        Process gate
                   any backend field change to a
                   launch-blocking ASIN"

preference        "Prefer to launch families            Strategic
                   simultaneously, not staggered"        decision
                  "Default goal regime is launch
                   velocity for first 60 days"
```

Position type affects how Atlas reads from the position:
- `strategic` → context in reasoning chain
- `style` → style block in NIS prompt
- `hard_refusal` → constraint enforced as filter (overrides
  even highest-confidence LLM output)
- `workflow` → triggers process gate / confirmation flow
- `preference` → soft input into Atlas verdicts

---

## Promotion flow (operator edit → position)

The accumulated-style approach we locked in: positions emerge
from edits, not from a starter block.

Every time the operator edits an NIS output:

```
EDIT DETECTED on Velune Pocket Family Title

Atlas generated:
  "Novelle Velune High-Rise 7/8 Leggings — Buttery-Soft
   Hidden Waistband Pocket, Athletic Fit"

You edited to:
  "Novelle Velune High-Rise 7/8 Pocket Leggings —
   Buttery-Soft Athletic Fit"

DIFF:
  - You moved "Pocket" earlier in title
  - You removed "Hidden Waistband"
  - You removed the comma before "Athletic Fit"

  This change pattern (move pocket reference earlier, drop
  the qualifier, prefer em-dash to comma) is new.

  Would you like to save any of these as operator_positions
  for future generations?

  ☐ Save: "Pocket reference belongs in first 5 words for
           pocket-family titles"
    scope: family:velune_pocket
    type:  style

  ☐ Save: "Prefer 'Pocket Leggings' over 'Hidden Waistband
           Pocket' in titles"
    scope: family:velune_pocket
    type:  preference

  ☐ Save: "Em-dash preferred over comma for title second
           clauses"
    scope: global
    type:  style

  ☐ Don't save any — this was a one-off edit
```

The detection LLM identifies candidate positions from diffs.
Operator accepts/rejects. Accepted positions write to substrate
and apply to future generations automatically.

---

## Velune-launch starter positions

These get created during M2 onboarding for Velune, derived
from the conversation today:

```
position #1
  scope: brand
  claim: "Athletic positioning only, no Casual"
  reasoning: "Brand position is premium-adjacent at $35-55;
              Casual register dilutes intent at this tier"
  type: strategic

position #2
  scope: brand
  claim: "No discount, value, or budget language in any
          content"
  reasoning: "Premium-adjacent positioning incompatible with
              value framing"
  type: hard_refusal

position #3
  scope: family:velune_pocket
  claim: "Family has hidden_waistband pocket; pocket details
          must be accurate in all content"
  reasoning: "Pocket vs no-pocket is the family-defining
              differentiator; accuracy prevents return-rate
              spikes"
  type: hard_refusal

position #4
  scope: family:velune_no_pocket
  claim: "Product Name must NOT include 'with Pockets' or
          imply pocket presence"
  reasoning: "Family is explicitly no-pocket; product name
              accuracy is launch-blocking"
  type: hard_refusal

position #5
  scope: brand
  claim: "Goal regime defaults to launch velocity for first
          60 days per ASIN, then transitions to margin
          unless operator overrides"
  reasoning: "Launch ranking is time-sensitive; margin
              optimization happens after rank stabilizes"
  type: workflow

position #6  (created when Mode 1 pricing Q&A runs first time)
  scope: brand
  claim: "Target CAC: [operator answers]"
  type: strategic

position #7  (same)
  scope: brand
  claim: "Willingness to operate at zero/near-zero contribution
          for first 30-60d for rank: [operator answers]"
  type: strategic

position #8  (same)
  scope: brand
  claim: "Anchor strategy: match CRZ premium pocket legging
          / undercut by $X: [operator answers]"
  type: strategic

position #9  (same)
  scope: brand
  claim: "Price psychology constraint: [operator answers
          or 'none']"
  type: hard_refusal
```

Positions 1–5 are seeded at M2 onboarding. Positions 6–9 emerge
from the first Mode 1 pricing Q&A flow on Day 5.

---

## Read path: how positions enter context

`build_context()` reads positions matching the decision:

```python
def read_active_positions(
    workspace_id: str,
    asin: Optional[str],
    family: Optional[str],
    decision_class: Optional[str],
) -> list[OperatorPosition]:
    """
    Read positions in priority order:
    1. scope=hard_refusal at any matching level (always apply)
    2. scope=asin matching this ASIN
    3. scope=family matching this family
    4. scope=decision_class matching this decision_class
    5. scope=family_decision_class compound matches
    6. scope=brand
    7. scope=global

    Conflicts: more-specific scope wins over less-specific.
    Equal-specificity conflicts: most recently created wins,
    with a warning logged for operator review.
    """
```

This is what makes "edit once, applies forever" real. The
operator's accumulated positions become layered constraints
that the LLM honors automatically.

---

## What this does NOT do (scope guard)

- Does NOT support multi-operator. Single operator locked in
  by design.
- Does NOT enforce positions via post-generation validation
  alone. Hard refusals go into the prompt as constraints AND
  are validated post-gen. Style/strategic positions go into
  prompt only; not validated.
- Does NOT auto-archive stale positions. Operator must review
  and archive explicitly. Stale-position detection is a future
  feature.
- Does NOT learn positions from outcome alone. Edits create
  positions; outcomes only adjust calibration weights on those
  positions later.

---

## Failure modes

1. **Position conflicts**: two positions of equal specificity
   contradict. The system picks the most recent and warns the
   operator. Operator must resolve manually.

2. **Position drift**: operator's stated position diverges from
   their actual edits. The system surfaces "you've edited
   against position #N in the last 3 generations — review the
   position?" (Quarterly position review prompt.)

3. **Over-fitting**: operator promotes too many style nits to
   positions early; later generations become brittle. The
   system caps style-type positions per scope (e.g., max 10
   active style positions per family) and asks operator to
   archive old ones before adding new ones.

---

## Version history

Append below this line.

- **v1.0 — 2026-05-18, present commit** — Initial design.
  operator_positions table specified. One-operator-per-brand
  locked. Five position types: strategic / style / hard_refusal
  / workflow / preference. Promotion-from-edit flow via
  diff-detection. Velune launch starter positions defined
  (#1-5 seeded at onboarding; #6-9 emerge from Mode 1 pricing
  Q&A). Read-priority order: specificity-first with conflict
  warnings.
