# PRICING_LOGIC.md — Operator-Rules Pricing Architecture

**Status:** design (pre-build)
**Author:** Devang / Atlas
**Date:** 2026-05-18
**Milestone:** M2 (Days 4–5)

---

## Purpose

The operator writes the floor rule and ceiling rule. The
dashboard executes them on every pricing decision. Atlas does
not invent floors or ceilings — those are operator-strategic
choices captured as substrate. Atlas computes the implications
of the operator's rules every time a price is set.

This is the architectural commitment that distinguishes Atlas
from a pricing recommender: Atlas does not recommend prices in
months 1-6. It records decisions, computes implications,
catalogs patterns. Calibrated recommendations come in Month 6+
once enough decision-outcome pairs accumulate. Mode 1 LLM
reasoning (uncalibrated) is available from Day 1 — explicitly
labeled as such.

---

## Two recommendation modes (explicit labeling required)

```
MODE 1 — LLM MARKET REASONING
  Available:   Day 1
  Source:      LLM general knowledge of category economics,
               competitor patterns, COGS math, brand
               positioning theory
  Quality:     moderate; thoughtful-consultant-level on Day 1
  Failure:    no Novelle-specific outcome data yet
  Labeling:    "Mode 1 — market reasoning, uncalibrated"

MODE 2 — CALIBRATED RECOMMENDATION
  Available:   Month 6+ (needs accumulated decisions + outcomes)
  Source:      observed lift/loss on Novelle's own pricing
               decisions, calibration_state per
               (decision_class, source, segment)
  Quality:    significantly higher than Mode 1 within brand
  Failure:    novel-decision blind, still bounded by
              segment evidence
  Labeling:    "Mode 2 — calibrated against [N] prior
               Novelle outcomes"
```

Until Month 6, every pricing recommendation Atlas shows is
labeled Mode 1. The operator decides. Atlas records.

---

## Schema

```sql
CREATE TABLE IF NOT EXISTS pricing_logic (
  workspace_id      TEXT NOT NULL,
  scope             TEXT NOT NULL,
                    -- 'global' | 'family' | 'asin'
  scope_ref         TEXT,
                    -- nullable when scope='global'

  floor_rule        JSONB NOT NULL,
                    -- structured rule (see below)
  ceiling_rule      JSONB NOT NULL,

  reasoning         TEXT,
                    -- why the operator chose this rule
  revision          INTEGER NOT NULL DEFAULT 1,
  set_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  set_by            TEXT NOT NULL,

  -- Ceiling reviewable cadence
  ceiling_next_review_at TIMESTAMPTZ,

  meta              JSONB DEFAULT '{}'::jsonb,
  PRIMARY KEY (workspace_id, scope, scope_ref)
);

CREATE TABLE IF NOT EXISTS pricing_decisions (
  decision_id       TEXT PRIMARY KEY,
  workspace_id      TEXT NOT NULL,
  asin              TEXT NOT NULL,

  price_set         NUMERIC(12, 2) NOT NULL,
  price_set_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  price_set_by      TEXT NOT NULL,

  -- Floor/ceiling state at decision time
  floor_at_time     NUMERIC(12, 2),
  ceiling_at_time   NUMERIC(12, 2),
  play_zone_position TEXT,
                    -- 'below_floor' | 'near_floor' | 'middle'
                    -- | 'near_ceiling' | 'above_ceiling'

  goal_regime       TEXT,
                    -- 'launch_velocity' | 'margin' | 'volume'
                    -- (defaults to launch_velocity for first
                    --  60 days per ASIN)

  reasoning         TEXT,
                    -- operator types why (or LLM captures
                    --  from Q&A flow)

  mode              TEXT NOT NULL,
                    -- 'manual' | 'mode1_llm' | 'mode2_calibrated'

  -- Outcomes attached later
  outcome_at_30d    JSONB,
  outcome_at_60d    JSONB,
  outcome_at_90d    JSONB,
  pattern_tags      TEXT[],

  meta              JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_pricing_decisions_asin
  ON pricing_decisions (workspace_id, asin, price_set_at DESC);

CREATE INDEX IF NOT EXISTS idx_pricing_decisions_regime
  ON pricing_decisions (workspace_id, goal_regime, price_set_at DESC);
```

---

## Velune floor rule (locked 2026-05-18)

```json
{
  "method": "variable_contribution_zero",
  "components": [
    "landed_cost",
    "fba_fee",
    "third_pl_fee",
    "referral_fee_15pct",
    "ad_spend_per_unit"
  ],
  "ad_spend_per_unit_source": "forecast_until_30d_actuals",
  "behavior_when_components_missing":
    "use_forecast_and_flag_in_provenance"
}
```

Computed every time a Velune ASIN is being priced. Returns
a number. Atlas refuses recommendations below this number
(hard refusal). Operator can override with explicit
acknowledgment that logs as `operator_override_below_floor`.

---

## Velune ceiling rule (locked 2026-05-18)

```json
{
  "method": "operator_manual",
  "anchor_reference":
    "CRZ Yoga premium pocket legging current price",
  "current_value": null,
  "current_value_source": "operator_typed",
  "review_cadence": "quarterly_or_on_demand",
  "next_review_at": "2026-08-18"
}
```

`current_value: null` until operator types it. When null,
Atlas shows pricing decisions without ceiling guidance but
does not block decisions. The ceiling becomes operative the
moment the operator enters a value (any time, weekly or as
they spot a CRZ price move).

---

## Pricing check (the function operator sees on every price set)

```
PRICING DECISION — Velune Pocket Leggings, Midnight Black, M

You're setting price to:  $39
                          [ Confirm ]  [ Cancel ]

YOUR ACTIVE RULES

  Floor rule:    variable_contribution_zero
  Floor right now: $32.15
  Your price:    $39   (+21% above floor)

  Ceiling rule:  operator_manual, anchored to CRZ
  Ceiling now:   (not set; operator hasn't entered yet)
  Your price:    $39   (ceiling not active)

  Play-zone position:  cannot compute without ceiling

AT $39, WITH CURRENT COSTS:
  Contribution per unit:    $5.20
  Implied TACOS for breakeven monthly volume:  23%
  Breakeven monthly volume:  ~100 units

GOAL REGIME (current):  launch_velocity
  Days since this ASIN's launch:  0
  Position in play zone (target):  bias near floor for
                                    ranking velocity

YOUR PRIOR DECISIONS IN THIS REGIME
  N=0 decisions logged so far (this is the first)

  No pattern yet. Pattern will be visible after ~5
  decisions in this regime.

ATLAS VERDICT
  Mode:        n/a — first pricing decision, no
               recommendation yet
  Atlas note:  Reminder: enter the CRZ ceiling whenever
               you spot it; this enables ceiling guidance
               going forward.

  [ Confirm $39 ]
  [ Ask Atlas for a Mode 1 recommendation ]
  [ Change rules ]
```

When operator clicks "Ask Atlas for a Mode 1 recommendation,"
the Q&A flow runs (see below).

---

## Mode 1 Q&A flow (first time per ASIN, or on-demand)

```
LLM MARKET REASONING — Velune Pocket Leggings

Before I propose a Mode 1 price, I need a few inputs that
aren't in substrate yet:

Q1   What's your target customer acquisition cost (CAC)
     for first-time Velune buyers?
     ____________________________________________

Q2   Are you willing to operate at zero or near-zero
     contribution for the first 30-60 days to build
     organic ranking velocity, or do you want every unit
     profitable from Day 1?
     ☐ Yes, willing to bleed for rank
     ☐ No, every unit profitable
     ☐ Hybrid: ☐ first N days bleed, then profit
     ____________________________________________

Q3   Anchor at CRZ Yoga's premium pocket legging price
     (match), or undercut by a stated amount?
     ☐ Match CRZ
     ☐ Undercut by $___
     ☐ Outprice CRZ by $___
     ☐ Don't anchor to CRZ — set independently
     ____________________________________________

Q4   Is there a price psychology threshold that matters?
     (e.g., must end in .99, must be under $40, must be
      above $35)
     ____________________________________________

(Each answer becomes an operator_position row scoped to
 brand or to the relevant decision_class.)

[ Submit and generate proposal ]
```

After Q&A, Atlas generates a Mode 1 proposal with the 5-layer
citation chain (same as content gen), labeled "Mode 1 — market
reasoning, uncalibrated."

The four Q&A items are the operator's chosen starter set
(confirmed 10:58 EDT, 2026-05-18). Operator can edit/replace
them in the dashboard before the first Velune ASIN is priced.

---

## When does Mode 2 turn on?

Mode 2 is calibrated against accumulated Novelle outcomes. It
becomes available when:

```
For a given decision_class (e.g., pricing_decision_within_family):
  - At least 30 closed pricing_decisions exist with outcomes
  - Outcomes span at least 60 days post-decision
  - Cohort comparison is computable (similar ASINs unchanged
    in same window)
  - Calibration_state for this class shows variance below a
    threshold (i.e., the system has actually learned something)
```

When these are met, the dashboard shows a Mode-2-available
banner. Operator opts in explicitly. First Mode 2 outputs
are clearly marked "first Mode 2 recommendation; calibration
quality unknown until 30 more outcome data points arrive."

---

## What this does NOT do (scope guard)

- Does NOT auto-update CRZ ceiling. Operator types it.
- Does NOT block pricing decisions when ceiling is null. Floor
  refusal is hard; ceiling guidance is informational.
- Does NOT auto-detect goal regime transitions. Default is
  launch_velocity for 60 days, then operator manually shifts.
- Does NOT do Mode 2 in months 1-5, even if outcome data
  accrues fast. Calibration threshold is per-decision-class
  and must be explicitly verified before Mode 2 surface
  exposes.
- Does NOT recommend price changes after the fact. Atlas
  surfaces "this ASIN's position in play zone has drifted"
  but doesn't propose new prices unless operator asks.

---

## Failure modes

1. **Floor rule incomplete inputs**: e.g., ad_spend_per_unit
   unknown for new ASIN. Behavior: use forecast (operator-
   provided at onboarding) and flag in provenance. When 30d
   actuals arrive, recompute floor; if new floor exceeds
   current price, surface "current price now violates floor
   — review."

2. **Operator override below floor**: hard refusal can be
   overridden with explicit acknowledgment. Override logs as
   a special event with reasoning required. If 3+ overrides
   in 30 days, dashboard surfaces "you've overridden below-
   floor 3 times — should we reconsider the floor rule?"

3. **CRZ ceiling stale**: operator typed CRZ price 60+ days
   ago. Aging rule auto-prompts review. Pricing decisions
   still computed with stale value but flagged "ceiling
   reference is N days old."

4. **Conflicting position from Q&A**: e.g., operator answers
   Q2 "every unit profitable" but later overrides below
   floor for rank-build. Dashboard surfaces the conflict and
   asks operator to update the Q2 position.

---

## Version history

Append below this line.

- **v1.0 — 2026-05-18, present commit** — Initial design.
  Operator-set rules architecture. Velune floor rule locked
  (variable contribution = 0). Velune ceiling rule locked
  (operator manual, CRZ-anchored, quarterly review). Mode 1
  LLM reasoning available Day 1. Mode 2 calibrated
  recommendations gated until month 6+ with explicit threshold
  criteria. Q&A flow (4 starter questions) runs first pricing
  per ASIN; answers become operator_positions. pricing_decisions
  table is the journal; pattern emergence visible after ~5
  decisions per regime.
