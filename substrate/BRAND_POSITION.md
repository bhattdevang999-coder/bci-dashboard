# BRAND_POSITION.md — Velune Position Locked

**Status:** design (pre-build), Velune position chosen
**Author:** Devang / Atlas
**Date:** 2026-05-18
**Milestone:** M2 (Days 4–5)

---

## Purpose

A single-row substrate primitive that names the brand's
strategic position: who it competes with, who it visually
references, what price tier it occupies, what the customer
hypothesis is. Every reasoning chain across content, pricing,
voice, and recommendation evaluation reads from this row.

Brand position is reviewed quarterly. It does NOT change
weekly. Daily decisions read from it; they don't change it.

---

## Velune position (locked 2026-05-18)

```
position_statement:
  "Premium-adjacent visual quality and voice register at
   $35–55 entry-tier price."

competitor_set:
  - lululemon
  - alo_yoga
  - crz_yoga
  - vuori
  - gymshark
  - halara

competitor_role:
  lululemon:    visual_anchor       ($98-128, aspirational reference)
  alo_yoga:     visual_anchor       ($88+, yoga-specific reference)
  crz_yoga:     direct_competitor   ($28-48, primary head-to-head
                                     for our shopper)
  vuori:        price_ceiling       ($45-65, do not drift into
                                     this tier)
  gymshark:     below_position      ($45-60, do not read like
                                     them in copy or imagery)
  halara:       below_position_price ($28-42, below price tier)

price_band:
  floor:         operator-rule (variable contribution = 0)
  ceiling:       operator-set, anchored to CRZ premium pocket
                 legging, manually entered weekly
  anchor_target: $42 (review quarterly)

positioning_hypothesis:
  "Target the premium-curious-but-budget-conscious shopper —
   someone who would buy Lululemon if money allowed, or who
   buys Lulu sometimes and CRZ otherwise. Aspirational visual
   + voice at one-third Lululemon price."

review_freq:    quarterly
next_review_at: 2026-08-18
```

---

## Schema

```sql
CREATE TABLE IF NOT EXISTS brand_position (
  workspace_id          TEXT PRIMARY KEY,

  position_statement    TEXT NOT NULL,
  competitor_set        TEXT[] NOT NULL,
  competitor_role       JSONB NOT NULL,
                        -- {competitor_id: role_string}
  price_band            JSONB NOT NULL,
                        -- {floor: rule, ceiling: rule,
                        --  anchor_target: number}
  positioning_hypothesis TEXT,

  -- Pricing rules linkage (see PRICING_LOGIC.md)
  pricing_logic_revision INTEGER,

  -- Cadence
  review_freq           TEXT NOT NULL DEFAULT 'quarterly',
  last_reviewed_at      TIMESTAMPTZ,
  next_review_at        TIMESTAMPTZ NOT NULL,

  -- Versioning
  revision              INTEGER NOT NULL DEFAULT 1,
  set_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  set_by                TEXT NOT NULL,

  meta                  JSONB DEFAULT '{}'::jsonb
);
```

One row per workspace. Updates bump `revision` and write a
parallel row in `substrate_events` with the prior content
preserved for audit.

---

## Reasoning impact (how Layer 2 reads this)

When NIS generates content for any Velune ASIN, the
brand_position row is injected into Layer 2 of the prompt:

```
LAYER 2 — STRATEGIC

  Brand position: Premium-adjacent visual quality and voice
                  register at $35–55 entry-tier price.

  Competitor frame:
    - Visual anchors: Lululemon, Alo Yoga (we look closer
                      to these than to budget brands)
    - Direct competitor: CRZ Yoga (our actual head-to-head
                          for the same shopper)
    - Below-position to AVOID sounding like:
        - Gymshark (gym-bro register, not our shopper)
        - Halara (price-driven, not premium-adjacent)
    - Price ceiling reminder: do not drift into Vuori tier
        ($45-65) without operator-explicit position change

  Customer hypothesis: premium-curious-but-budget-conscious;
                       Lululemon-curious shoppers trading down,
                       or CRZ-loyal shoppers trading up

  Anchor target price: $42 (current quarter)
```

This shapes every voice choice, every pricing decision, every
recommendation verdict.

---

## Quarterly review trigger

When `next_review_at` arrives (or operator triggers manually):

```
QUARTERLY POSITION REVIEW

  Last set:        2026-05-18
  Time since:      90 days
  Decisions made
  under this
  position:        ~150 (NIS, pricing, recs combined)

  Outcomes
  attributable
  to this position:
    Sessions     12% above pre-position baseline
    Revenue      19% above pre-position baseline
    Returns      3% below pre-position baseline
    Organic %    62% (within guardrail)
    TACOS        28% (within guardrail)

  Calibration check:
    Prior similar brands at this position succeeded in
    [X] of [Y] cohort observations.

  Recommend:
  ☐ Keep position as-is (default)
  ☐ Adjust price band (raise / lower)
  ☐ Re-evaluate competitor set
  ☐ Pivot position (significant change — re-onboard
    flow triggered)
  ☐ Defer review by 30 days
```

The review forces explicit operator engagement. Position
drift without intent is the failure mode this prevents.

---

## What this does NOT do (scope guard)

- Does NOT auto-update from market data (CRZ price changes,
  competitor moves). Position is operator-strategic, not
  market-reactive.
- Does NOT support per-family position differentiation. Velune
  has ONE position covering both pocket and no-pocket families.
  Multi-position brands are a future architectural choice.
- Does NOT trigger autonomous voice/pricing changes when
  position changes. Position changes prompt operator to review
  and reapprove derivative artifacts.

---

## Velune position — initial decision rationale

(Captured here as substrate of the decision itself, per the
"reasoning matters more than answers" principle.)

The operator chose Option A (CRZ-style) over B/C/D after
explicit pushback in chat. Reasoning:

1. **Price tier is winnable.** $35–55 is below Lulu/Alo and
   above commodity. Real demand pattern proven by CRZ Yoga's
   business.

2. **Visual quality is achievable on COGS.** 79% Nylon /
   21% Spandex at ~$10-12 landed cost can support $42 retail
   with $20+ contribution AND fund photography/packaging
   upgrades.

3. **CRZ has revealed the playbook.** Solid-color minimalism,
   lifestyle-not-gym imagery, voice avoiding both Lulu's
   preciousness and Gymshark's hype.

4. **Position attacks losable flanks.** Halara (visual quality
   weak), Gymshark (yoga/lifestyle weak), Old Navy Powersoft
   (premium signal weak) are vulnerable to a CRZ-style brand.

5. **Calibration is tractable.** Cohort comparison against CRZ,
   weekly competitor monitoring of 3-5 SKUs, A/B tests against
   their structures all achievable.

The substantive risk: this position assumes the premium-curious
shopper is the real Velune buyer. If actual buyers turn out to
be price-driven Amazon shoppers, position is wrong and should
shift to "best value at $32-42." First 60 days of outcome data
will signal which segment buys.

---

## Version history

Append below this line.

- **v1.0 — 2026-05-18, present commit** — Initial position
  locked. Velune at $35-55, CRZ-adjacent, 6-competitor frame.
  Quarterly review cadence. Single-row schema. Positioning
  hypothesis stated explicitly so it can be validated against
  outcome data at first review (Aug 18, 2026).
