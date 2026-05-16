# Unit Economics — Audit + Gap Map (Step 1)

> Read-only doc. No code yet. Maps what cost/revenue/spend infrastructure exists for a per-ASIN contribution-margin ledger and what the gaps are. Same prerequisite pattern as `substrate/BRAND_VOICE.md`.
> Audit date: 2026-05-16, SHA `497a239`

This file is the prerequisite for the observation substrate (Phase B), the sensitivity-testing calculator (Phase C), and any later price-range observation work (Phase D).

---

## What the operator is actually asking for

Not a pricing recommender. A **per-ASIN unit-economics ledger** that:

1. Captures **cost inputs the operator already knows** (landed cost, warehouse/3PL fees, return cost) — these have to come from the operator because they live in supplier invoices and 3PL contracts, not in any Amazon report
2. Pulls **revenue and spend signals from Amazon data** that's already flowing through Atlas (sales reports, PPC bulks, returns)
3. Computes **contribution margin per ASIN per period** at the actual prices the ASIN sold at
4. Lets the operator run **"what if I priced at $X" sensitivity tests** — no recommendation, just honest math against the current cost structure

**Atlas never recommends a price.** Atlas computes margin against operator-supplied costs and operator-chosen what-ifs. This is the safety boundary that distinguishes a useful unit-economics module from a dangerous "Atlas suggests $X" feature that could lose the Buy Box.

---

## Substrate audit: what's already there

| Concern | Where it lives today | Notes |
|---|---|---|
| `outcome_events` table | live, recognised metrics: `search_volume`, `organic_rank`, `acos`, `spend`, `impressions`, `clicks`, `orders`, `ctr`, `cvr` | The whitelist is in `substrate/marketing.py:_OUTCOME_METRICS`. Extending it is a single-line code change, no schema migration. |
| `pre_change_snapshot` | already captures whatever outcome data exists per ASIN at decision time | Once price/units/refund_amount land in outcome_events, they automatically appear in NIS / Variations / Brand Voice snapshots. |
| `ingestion_records` | already audits every uploaded file | Adding `file_kind='cost_inputs'` or per-3PL fee uploads is a free-form string in this column. No schema work. |
| `SALES_FIELD_MAP` (app.py:8063) | already parses business reports for `sessions`, `units`, `revenue`, `returns`, `return_rate`, `buy_box_pct`, `ad_spend`, `ad_revenue`, `acos`, `rank` | All the revenue-side signal we need. Parser exists. |
| `/api/catalog/upload-sales` | already accepts business-report files | But **only writes to `catalog_health_state` (in-memory)**. **It does NOT push into `outcome_events`.** This is the single biggest gap for the unit-economics module to function. |
| `_OUTCOME_METRICS` whitelist | drops anything not in the tuple | Same gap: even if sales upload tried to write `units` or `revenue` to outcome_events, the whitelist would silently reject them. |
| Cost fields in `brand_configs/*.json` | **NONE** — only operational defaults (`default_care`, `default_upf`, vendor code) | No landed cost, no MAP, no margin target, no overhead allocation anywhere. |
| Cost fields in `brand_profile` table | **NONE** | Voice fields only. |
| Per-ASIN cost storage | **NONE** | The operator has nowhere in Atlas to type "Style 0001 costs $14.22 landed." |

**Headline:** the substrate plumbing (outcome_events, snapshots, ingestion audit) is built. The **revenue-side data is in the system but doesn't reach the substrate** (sales-report uploads dead-end in memory). The **cost side has no home anywhere** — not in JSON, not in `brand_profile`, not in any table.

---

## Cost-allocation model (the decision that has to come first)

Different brands answer "what's the unit cost?" differently. **Atlas has to pick one model and be explicit about it**, otherwise the margin numbers are meaningless. Two reasonable models:

### Model 1: Variable costs only (recommended)

**Per unit:**
- Cost of goods (landed cost — supplier price + freight + duties to FBA)
- FBA fulfillment fee (from Amazon's fee table per size tier)
- 3PL pick-pack fee (if applicable)
- Variable returns cost = return rate × refund amount per unit
- Optional: per-unit promotional discount applied (coupons, Subscribe & Save)

**Above the line (brand-level, not allocated):**
- Photography, A+ design, content rewrites
- Tooling, samples, R&D
- Operator time / agency fees
- Software / Helium 10 / Atlas itself

**Pros:** This is the textbook contribution-margin model. SKU-selection decisions ("which to expand") work correctly. Per-ASIN numbers are comparable across the catalog.

**Cons:** Brand-level "is this business profitable" requires the operator to subtract fixed overhead themselves after the rollup.

### Model 2: Fully-allocated costs

Same as Model 1, plus a per-unit share of fixed overhead (overhead ÷ unit volume).

**Pros:** Operator sees one bottom-line margin per ASIN.

**Cons:** The per-unit allocation **changes when total volume changes**, which means a slow ASIN looks unprofitable purely because of allocation, not unit economics. Bad for SKU-selection decisions. Industry consensus is "don't allocate fixed costs to unit decisions."

### Recommendation

**Model 1: variable costs only at the unit level.** Fixed overhead displays at the brand-level rollup as a single line above the contribution-margin total. Operator can mentally subtract to see net brand profit. Atlas doesn't pretend to know how to allocate $50K of photography across 40 SKUs.

---

## Ad spend treatment (the other decision)

Operators use two different formulas every day and they conflict:

| Formula | Numerator | Denominator | What it answers |
|---|---|---|---|
| **Pure contribution margin** | revenue − COGS − fulfillment − returns | revenue | Does this ASIN make money before marketing? Used for SKU-selection. |
| **TACOS (Total ACOS)** | ad spend | total revenue | What % of revenue did we plow back into ads? Used for marketing efficiency. |
| **Net margin after ads** | revenue − COGS − fulfillment − returns − ad spend | revenue | Is this ASIN paying for itself this month? Used for cash-flow decisions. |

### Recommendation

**Show all three columns side by side, clearly labeled.** Don't make Atlas pick one — the operator wants different ones for different decisions. The substrate already separates `spend` from `revenue`, so the math is trivial; the work is just labeling clearly.

---

## MAP enforcement

Many brand contracts have a Minimum Advertised Price (MAP) — the floor below which the brand can be terminated by Amazon or sued by upstream retailers. Atlas absolutely should never recommend a price below MAP, but **today there is no field anywhere in Atlas that stores MAP per ASIN**.

### Recommendation

Add **`map_price` as a per-ASIN field** in the cost-inputs module (alongside landed_cost, fba_fee, etc.). Any future sensitivity test or recommendation that would land below MAP gets a hard refusal: "below MAP, not displayed." This is a safety-rail decision, not a feature.

---

## The data Atlas has vs. doesn't, in one table

| Field | In Atlas now? | Source | Frequency |
|---|---|---|---|
| `revenue` (per ASIN per period) | yes (parsed from sales report, but dead-ends in catalog_health_state) | Amazon Business Report | weekly / monthly operator-driven upload |
| `units_sold` (per ASIN per period) | yes (parsed, dead-ends in memory) | same | same |
| `sessions` (per ASIN per period) | yes (parsed, dead-ends in memory) | same | same |
| `returns_count` / `return_rate` | yes (parsed, dead-ends) | Amazon Returns report (separate file_kind already recognized) | monthly |
| `ad_spend` (per ASIN per period) | yes (parsed from sales report; also flows through PPC bulk → outcome_events) | sales report OR ppc_bulk join | weekly |
| `ad_revenue` (attributed) | yes (parsed from sales report, dead-ends) | sales report | weekly |
| `buy_box_pct` | yes (parsed, dead-ends) | sales report | weekly |
| `landed_cost` per ASIN | **NO** | operator-supplied | one-time, revise on cost change |
| `fba_fee` per ASIN | **NO** | operator-supplied or Amazon fee preview API | one-time per size tier change |
| `3pl_fee` per unit | **NO** | operator-supplied flat or per-unit | per 3PL contract change |
| `map_price` per ASIN | **NO** | operator-supplied (brand contract) | one-time, rarely revised |
| `refund_amount` per return | **NO** explicitly; rolled into return_rate | derive from Returns report or operator estimate | monthly |
| `promotional_discount` per ASIN per period | **NO** | Brand Analytics or operator estimate | period-by-period |
| `competitor_price` per ASIN | **NO** | Brand Analytics, Helium 10, or scrape | daily / weekly |

**Read of the table:**
- The **revenue side is 7/7 covered** by data Atlas already extracts but doesn't yet push into the substrate
- The **cost side is 0/4 covered** — operator-supplied across the board, with nowhere to type it
- The **competitor / promo side** is genuinely missing data that requires external feeds

---

## What's blocked on operator input vs. blocked on code

| Blocker | Items |
|---|---|
| Operator typing (10-30 min per brand) | landed_cost, fba_fee, 3pl_fee, map_price, fixed_overhead_monthly, return refund estimate |
| Code (small) | Push sales-report data into outcome_events; extend `_OUTCOME_METRICS` whitelist; add cost-inputs storage + UI |
| Code (medium) | Per-ASIN-per-period contribution margin rollup; sensitivity calculator |
| External data feed | Competitor pricing (Brand Analytics or paid tool); promotional discount detail |

The operator-typing blocker is fine: cost data has to come from them anyway. **The critical code unblock is wiring the sales upload into outcome_events.** Until that lands, the unit economics module has revenue numbers that exist in memory but not in the substrate, and `pre_change_snapshot` can't capture them.

---

## Sequencing (proposal — needs operator sign-off)

### Phase A (this doc — DONE)
Audit + decisions. No code.

### Phase B — Substrate plumbing
Small but high-leverage commit:
1. Extend `_OUTCOME_METRICS` whitelist with `revenue`, `units_sold`, `return_amount`, `buy_box_pct`, `ad_revenue` (and any others surfaced during Phase B build)
2. Modify `/api/catalog/upload-sales` to also write these to `outcome_events` per ASIN per period (not just memory)
3. Add `Module.UNIT_ECONOMICS` enum
4. Substrate test: after sales upload, outcome_events has the right rows

**Result:** every business-report upload populates the substrate. Pre-change snapshots automatically include the latest sales metrics. Phase 2 attribution gets revenue data for free. No UI yet.

### Phase C — Cost inputs UI + storage
1. New `cost_inputs` table or extension of `brand_profile.custom` (table is cleaner per-ASIN, custom is easier per-brand)
2. New "Costs" page in the sidebar OR sub-tab under Brand Voice
3. Per-ASIN form: landed_cost, fba_fee, 3pl_fee, map_price + brand-level: fixed_overhead_monthly
4. Every save writes a `decision_event` with `module='unit_economics'`, `field_name='cost_input'` so the operator's cost history lands in Memory
5. The audit trail catches "we lost margin because someone changed landed cost without telling anyone"

### Phase D — Margin rollup view
1. Per-ASIN-per-month contribution margin table joining cost_inputs + outcome_events
2. Three margin columns side by side: contribution / TACOS / net-after-ads
3. Honest about gaps: "$0 cost on file for this ASIN" displays as a warning, not a zero
4. Drill into any cell to see the underlying inputs

### Phase E — Sensitivity calculator
1. Pick an ASIN + a hypothetical new price
2. Atlas holds costs and ad spend constant, recomputes margin
3. **MAP refusal**: hypothetical below `map_price` displays "below MAP, not allowed" instead of a number
4. Optionally: model a CVR change ("if CVR holds at current %, units stay flat; if CVR drops 30%, here's what the new units × new margin looks like")

### Phase F (much later) — Price-range observations
"In May, when this ASIN sold at $42, units = 120, CVR = 4.1%. In June at $48, units = 90, CVR = 3.2%." **No recommendation.** Just the ASIN's own history reflected back. Same Confound-view discipline.

---

## Honest hard parts I want to surface

1. **The "lifetime amortization" question.** Tooling and photography are real costs that happen once and serve a SKU forever. Model 1 puts them above the line. The operator might want to amortize them over a projected lifetime instead. Industry convention is "don't" (lifetime is unknowable) but it's a real conversation. I'd ship Model 1 and revisit only if operators ask.

2. **What "period" means.** Weekly business reports overlap with calendar months. A month-ending rollup wants a month-aligned period; a weekly trend wants the report's own period. The substrate stores `observed_at`; the rollup query has to make the period choice at read time. Solvable but worth being explicit: the rollup is **month-aligned, computed on demand,** not pre-aggregated.

3. **The "what if PPC changed" sensitivity.** The simplest sensitivity holds ad spend constant. But pricing changes often coincide with PPC strategy shifts — the operator drops price and lowers bids together. Modeling that joint move is more honest than a single-dimension what-if, but it's also a much bigger feature. Phase E ships single-dimension (price only); joint-move sensitivity is a later thing.

4. **The "competitor moved" confound.** Margin at $42 means one thing if competitors are also at $42 and a different thing if competitors moved to $36. Without competitor pricing data, Atlas can't show this context. We can ship Phase B-E without it — but the operator should know they're flying with one eye closed on competitive position until competitor pricing lands.

5. **Returns are lagged.** A unit sold in May doesn't show up as a return until June or July. So May's true margin isn't knowable in May; it solidifies 60-90 days later. The rollup should show **"as-of" margin (returns we know about)** plus an **estimate-with-return-rate-applied** version. Same principle as the Confound view — present both, never claim certainty.

---

## Open questions for explicit input before Phase B

1. **Cost allocation model:** Model 1 (variable only, fixed above the line) or Model 2 (fully-allocated)? My recommendation: Model 1.
2. **Ad spend treatment:** show all three columns (contribution / TACOS / net-after-ads)? My recommendation: yes, all three.
3. **MAP refusal:** hard refusal in the sensitivity calculator, or warning the operator can override? My recommendation: hard refusal — operators forget MAP at midnight and the brand-termination cost is too high.
4. **Cost-inputs storage:** new `cost_inputs` Postgres table keyed by `(workspace_id, asin)`, or wedge into `brand_profile.custom`? My recommendation: new table. Per-ASIN data doesn't belong in a brand-level profile; the table lets us version cost changes the same way we version voice.
5. **Returns:** track as `return_rate` (a % the operator estimates per ASIN) or model the actual returns lag from the Returns report? My recommendation: support both — operator-entered rate is the Day-1 fallback, actual returns override it once the substrate has data.
6. **Editor placement:** new "Costs" or "Unit Economics" sidebar item, or sub-tab under Brand Voice (since both are operator-supplied brand data)? My recommendation: new sidebar item under Core, in the same row as Brand Voice. Unit economics is large enough to deserve its own surface.

---

## Version history

Append below this line. Do not edit entries above.

- **v1.0 — 2026-05-16, present commit** — Initial audit. Documented that the revenue side of unit economics is 7/7 covered by existing parsers but dead-ends in `catalog_health_state` instead of `outcome_events`. Cost side is 0/4 covered — no field exists anywhere in Atlas for landed cost, FBA fee, 3PL fee, or MAP. Proposed 5-phase sequencing (substrate plumbing → cost inputs → margin rollup → sensitivity calculator → price-range observations). Six open questions surfaced for explicit input before Phase B.
- **v1.1 — 2026-05-16, prior commit** — **Phase B shipped.** New module `substrate/unit_economics.py` with `record_sales_observations()`: takes parsed business-report rows + the `sales_fields` map and pushes one outcome_events row per (ASIN, metric) per period. Nine metrics now durable in the substrate: `revenue`, `units_sold`, `sessions`, `returns`, `return_rate`, `cvr`, `buy_box_pct`, `ad_spend`, `ad_revenue`, `acos`. `_OUTCOME_METRICS` whitelist in `substrate/marketing.py` widened to share the same vocabulary so reads stay uniform. `Module.UNIT_ECONOMICS` enum added. `/api/catalog/upload-sales` now writes both an `ingestion_records` row and the outcome_events batch — best-effort, never blocks the upload response. Cell parser handles `$`, `,`, `%`; empty cells are **skipped**, not zero-filled; rows with no ASIN are skipped (counted in `skipped`). `period_start` / `period_end` populated when the report carries them. 38 QA assertions in `qa_unit_economics.py`, all 11 previously-green QA suites still green, 32/32 substrate unit tests still green. Phases C–F (cost inputs + UI, margin rollup, sensitivity calculator, price-range observations) remain blocked on operator confirmation of the six open questions above + operator-supplied cost data.
- **v1.2 — 2026-05-16, present commit** — **Phase C + D shipped.** Two new tables (`cost_inputs`, `brand_overhead`) and the schema version bumped to v5. New module `substrate/cost_inputs.py` with `read_cost_input` / `list_cost_inputs` / `save_cost_input` (per-ASIN) and `read_overhead` / `save_overhead` (brand-level). Every cost save writes a `decision_event` with `module='unit_economics'` so cost history lands in Memory; revision integer bumps on every write. New module `substrate/margin.py` with `margin_rollup()` joining `cost_inputs` + `outcome_events` and producing three margin columns per row — `contribution_margin_per_unit`, `tacos`, `net_after_ads_per_unit` — plus blended totals (revenue / ad spend / TACOS / contribution × units / net-after-overhead). Latest-wins per (ASIN, period, metric) so re-uploads of the same business report don't double-count. Contribution is `None` (not zero) when any required cost field is missing; MAP warning fires when avg selling price < MAP. Five endpoints under `/api/atlas/unit-economics/*` (costs list, cost get/save per ASIN, overhead get/save, margin rollup with period/asin filters). Frontend: new `Unit Economics` sidebar item under Core with three tabs (Margin rollup / Cost inputs / Brand overhead). Margin table colors negative contribution/net red, TACOS > 40% red, missing-cost rows get a warning stripe. 49 QA assertions in `qa_unit_economics_costs.py`, all 12 QA suites still green. **Phase E** (sensitivity calculator with hard MAP refusal) and **Phase F** (price-range observations) remain.
