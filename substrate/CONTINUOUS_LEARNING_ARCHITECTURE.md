# Atlas — Continuous Learning Architecture

Operator: Devang. Brand: Novelle (activewear). Scale at start: 40 ASINs, all new launches, single seller account, ~10 days from May 18 2026.

This doc covers how Atlas learns over time — five loops, what each loop actually does, what data it needs, where the UX lives, how it fails, and the honest 18-month build sequence with confidence intervals. It is descriptive of the *target*, not a promise of timing. Where intervals are wide, they are wide because the prerequisite data does not exist yet.

This doc lives next to MODULES.md and follows the same append-only version-history convention.

---

## Objective and KPI tree

**Primary objective:** `contribution_margin_dollars` per month.

This is `revenue − (landed_cost + fba_fee + 3pl_fee + 15% referral_fee + ad_spend)`, summed across all ASINs, rolled to month. The `substrate/margin.py:margin_rollup` already computes this; Atlas does not need a new metric.

**Guardrails (hard refusals or escalations when breached):**

| Guardrail | Threshold | Refusal type | First-60-day launch override |
| --- | --- | --- | --- |
| `organic_unit_share` | ≥ 60% | escalate | ≥ 35% (rank chase eats organic) |
| `return_rate` | ≤ 8% | escalate | same |
| `avg_selling_price` | ≥ MAP | hard refusal | hard refusal (no leniency) |
| `TACOS` | ≤ 35% | escalate | ≤ 55% (launch velocity regime) |
| `inventory_weeks_on_hand` | ≥ 4 | escalate | ≥ 3 (newer SKUs run leaner) |

The launch_mode_until column on `asin_metadata.meta` (or a dedicated column once usage stabilizes) marks when these thresholds revert to steady-state.

**KPI tree (read top-down when an outcome shifts):**

```
contribution_margin_dollars
├── revenue
│   ├── units_sold              ← Loop 2 posterior (price elasticity, content lift)
│   │   ├── sessions            ← Loop 1 (NIS quality), Loop 5 (drift on traffic)
│   │   ├── cvr                 ← Loop 1, Loop 2 (price ↔ cvr per ASIN)
│   │   └── buy_box_pct         ← steady-state assumption; flag if drops
│   └── avg_selling_price       ← Loop 2 posterior, ceiling+floor rules
└── variable_cost_per_unit
    ├── landed_cost             ← cost_inputs, manufacturer side
    ├── fba_fee / 3pl_fee       ← Amazon-set; we just track
    ├── referral_fee_15pct      ← Amazon-set
    └── ad_spend_per_unit       ← marketing budget loop, Loop 2 (campaign quality)
```

---

## The five learning loops

Loop 3 is **out of scope until brand 2 onboards.** It is documented here because it changes how Loops 1, 2, and 4 are built (we need to avoid baking in single-brand assumptions). Anything written here about Loop 3 is design intent only.

### Loop 1 — NIS preference model (operator edit pairs)

**Purpose.** Learn what Devang considers a good listing. Every time Atlas emits a NIS output (title, bullets, description, A+, image brief) and Devang edits before approving, the (original, edited) pair is a labeled training signal for tone, structure, and factual emphasis preferences.

**Signals captured.**
- `original_output` (cited NIS payload, including `confidence_breakdown`)
- `operator_edit` (final approved value)
- `decision_class` (title_generation, bullet_generation, etc.)
- `scope_keys` (asin, family, decision_class) — used to promote style positions later
- `edit_distance` and a parsed `diff_categories` (added_factual_claim, removed_marketing_phrase, restructured_bullet_order, etc.)
- `time_to_edit` (how long the operator dwelled — a proxy for friction)

**Model.** Two-stage, not one:
1. *Diff classifier* — a small fine-tuned model that labels diff categories from raw text. Cheap, can run on every approval. Catches the obvious patterns.
2. *Style position promotion* — when a diff category recurs across 3+ ASINs at the same scope, propose an `operator_position` row (`position_type='style'`) and queue it in the Operator Positions sidebar for explicit confirmation. We do NOT auto-promote silently. The operator is the only one who promotes.

**Data plumbing.**
- Write to a new `nis_edit_pairs` table (not yet shipped). Schema sketch: `pair_id`, `workspace_id`, `decision_class`, `original`, `edited`, `diff_payload`, `edit_distance`, `time_to_edit`, `approved_at`, `scope_keys`.
- The citation_chain already stores `decision_event_id`; the edit pair references that id so we can rerun verdicts after a style position is promoted (sanity check that the new position would have caught the original).

**UX surface.**
- Cited NIS page: existing edit workflow becomes the labeled-data flow with no new screen needed.
- Operator Positions page (already shipped): new section "Proposed style positions from your edits" — operator confirms/rejects/parks each one.
- A small "edit pattern of the week" weekly digest once we have ≥ 20 edits; until then, suppressed.

**Failure modes.**
- *Small-N overconfidence.* 40 ASINs × few NIS regenerations each = maybe 200-400 edit pairs in the first 90 days. Not enough to fine-tune anything serious. Loop 1 stays at heuristic + rules until ~500 labeled pairs (confidence interval: 4-9 months).
- *Operator drift.* Devang's taste changes over time. Promoted positions need a `last_reaffirmed_at` check; stale positions get a quarterly review prompt.
- *Adversarial pattern.* If a diff category is "operator removed an honest claim because it looked weak," we don't want to learn to remove honest claims. We need a manual review pass before the *third* recurrence triggers promotion.

**Confidence at launch:** **35% chance Loop 1 produces a useful style position by Month 4.** Higher only if Devang's edit volume is consistent and the substrate captures clean diffs.

### Loop 2 — ASIN-level decision posterior

**Purpose.** Learn the relationship between operator-controllable inputs (price, content, ad spend, images) and outcomes (CVR, units, organic share, return rate) at the ASIN level. This is the loop that lets Atlas eventually answer "should we raise price $2?" with anything more than a guess.

**Signals captured.**
- All `outcome_events` rows (already shipped via UNIT_ECONOMICS Phase B)
- All `pricing_decisions` rows + 30/60/90-day outcome attachments (shipped in M2)
- All approved NIS outputs with their `decision_event_id` and timestamp
- `competitor_state` observations as covariates (CRZ price moves, etc.)
- `budget` rows (theme allocation, monthly spend)
- Confound markers: launch_mode flag, seasonality, deal events, holiday windows

**Model.** Bayesian hierarchical, ASIN-level posteriors with brand-level priors. Concretely:
- Per-ASIN posterior over price elasticity (slope of `log(units) ~ log(price)`)
- Per-ASIN posterior over content-change effect (NIS regeneration → CVR delta)
- Per-ASIN posterior over ad-spend efficiency (incremental TACOS effect)

Cold-start prior is the brand-level pooled estimate (so all 40 launches start with the brand's average elasticity, then update with their own data). When brand 2 onboards, the prior becomes a Loop 3 meta-prior.

**Data plumbing.**
- Outcome attachments on `pricing_decisions` are already wired (M2). We need a corresponding hook from approved NIS edits → `outcome_events` for the content-change loop. Not yet shipped.
- A scheduled job (likely the same daily-cycle cron) runs the posterior update overnight. Outputs land in a new `decision_posteriors` table (not yet shipped) keyed by `(workspace_id, asin, decision_class, posterior_version)`.

**UX surface.**
- Pricing page (shipped): the existing decision-log gains a "calibrated suggestion" column once Mode 2 turns on (≥ 60 days of outcome data per ASIN).
- A new "Insights" page (not yet shipped) renders posteriors as plots — but this is at least 3 months out because the posteriors themselves need data first.

**Failure modes.**
- *Confounded data.* New launches simultaneously change price, content, image, and ad spend. Each individual effect is unidentifiable. Need a basic identification strategy: stagger changes by ≥ 7 days where the operator can, or accept the "joint change" verdict in the posterior.
- *Survivorship bias.* If we archive losing ASINs, our posteriors become rosier than reality. We need to keep archived ASINs in the model with their final-period outcomes.
- *Mode 2 false-precision.* The biggest risk is shipping a confident "raise price $2" recommendation when the posterior is actually wide. Mode 2 must surface the 80% interval, not the point estimate, and must refuse to recommend when interval crosses zero.

**Confidence at launch:** **45% chance Loop 2 has actionable posteriors on ≥ 10 ASINs by Month 9.** Mode 2 calibrated pricing is explicitly gated to Month 6+, and even that's optimistic.

### Loop 3 — Cross-brand pooling / meta-prior (OUT OF SCOPE until brand 2)

**Purpose (when it's in scope).** Use signal from multiple brands to build a stronger prior for new-brand launches. The promise is that brand N+1's first 40 launches start better-informed than brand 1's did.

**Why it's out of scope now.** With one brand, "pooling" is just per-ASIN modeling. We cannot avoid baking in Novelle-specific assumptions silently; the best we can do is *flag* every place we made an assumption that won't generalize, so when brand 2 onboards we know what to refactor.

**Design intent to preserve.**
- All Loop 2 model parameters carry a `pooling_eligible: bool` flag. Brand-specific things (Velune's CRZ-anchored ceiling, the "no Casual" hard refusal) are `false`; product-class things (legging elasticity, athletic-apparel return rate distribution) are `true`.
- `brand_position` already exists per workspace; the meta-prior will be parameterized by brand position fields (price tier, competitor frame, hypothesis), not by brand_id.

**Confidence:** N/A until trigger.

### Loop 4 — Active learning

**Purpose.** When Atlas has equipoise (the posterior is wide enough that the answer matters), propose a test to the operator. The point is not to A/B test everything; it's to spend operator attention only where the expected information gain justifies it.

**Signals captured.**
- `pricing_decisions` posterior widths
- `decision_posteriors` per-ASIN posterior widths (once Loop 2 has data)
- `unknowns` rows where `evidence_path='a_b_test'`
- Operator capacity (how many tests can Devang run per month — we ask, we don't assume)

**Model.** Bayesian optimal experimental design — choose the test that maximally reduces variance on the contribution-margin decision. In practice, a heuristic ranking by `posterior_width × decision_leverage × test_feasibility` will get us most of the way for the first year.

**UX surface.**
- A "Tests Atlas wants to run" inbox (not yet shipped). Each proposed test: hypothesis, design, expected information gain (in dollars of contribution margin clarity), operator effort estimate. Operator approves, runs, attaches outcome.

**Failure modes.**
- *Test fatigue.* If Atlas proposes 5 tests/week, Devang ignores them all. Cap at ≤ 2 active tests at any time, and rank by leverage.
- *Confounded tests.* See Loop 2. Same problem; same mitigation (don't propose a test on an ASIN actively running another change).
- *Operator says no, Atlas keeps asking.* When a proposed test is declined twice, retire it for 90 days.

**Confidence at launch:** **20% chance Loop 4 proposes its first useful test by Month 6.** Higher if Loop 2 stabilizes faster than expected.

### Loop 5 — Drift detection

**Purpose.** Notice when something Atlas has been treating as stable starts moving — competitor price shift, BSR drift, conversion rate decay, seasonality change, organic-share erosion. Surface it before the operator has to find it manually.

**Signals captured.**
- `competitor_state` (already shipped, manual writes today)
- `outcome_events` time series per ASIN
- `pricing_decisions` outcome attachments
- `recommendation_ingest` patterns (agencies often surface drift before we detect it; treat their recs as a drift signal even when we disagree on the fix)

**Model.** Per-signal control charts (EWMA or page-hinkley for fast detection; CUSUM for sustained shifts). Each signal has a `drift_window` and a `drift_threshold` parameter; both start as defaults and get tuned manually for the first 6 months.

**Data plumbing.**
- A new `drift_alerts` table (not yet shipped). Fields: `alert_id`, `signal`, `asin?`, `direction`, `magnitude`, `window`, `severity`, `acknowledged_by`, `linked_decision_id`.
- Daily cron evaluates control charts on overnight data.

**UX surface.**
- A `drift_alerts` sidebar item with severity-sorted feed. Operator either acknowledges (closes alert), opens a decision (Atlas drafts the response), or marks as noise (tunes the threshold).

**Failure modes.**
- *Alert fatigue.* 40 ASINs × N signals each × daily eval = thousands of potential alerts. Defaults must be conservative; severity must compound across signals before alerting.
- *Manual `competitor_state` is sparse.* Drift detection on CRZ price can't fire if no one's checking CRZ weekly. We need a manual-entry cadence reminder (every Monday morning) or a Helium10/Keepa pull (vendor connector, not yet built).
- *Coincident-with-launch noise.* New ASINs always look "drifting" because they have no baseline. Suppress drift alerts until 30 days post-launch per ASIN.

**Confidence at launch:** **60% chance Loop 5 catches its first non-trivial drift event in the first 30 days of multi-ASIN operation** — but only if the signals it watches actually have data (which today, beyond pricing_decisions, they mostly don't).

---

## Build sequence — 18 months

Honest dates only. Each month gets one primary deliverable. Confidence intervals reflect the assumption that no other priority displaces this work. Operator capacity is the binding constraint, not engineering capacity.

### Month 0 (now, May 18 2026)
- **Phase 1.5 sprint** (M1+M2+M3+M4+M5+M5b) — shipped to master. Substrate + UX for context, citations, unknowns, mode-aware entry, Velune onboarding, recommendation ingest, content benchmarks.
- **M6 sprint** (Days 1, 1.5, 2, 2.5) — shipped to master. Catalog audit substrate (7 new tables in schema v10: brand_workspace, audit_rules, cohort_classifications, catalog_audit_findings, audit_decisions, audit_sessions, analytics_views). Async ingest jobs (schema v11). XLSX-to-substrate ingest pipeline with active/dormant/unknown cohort classification, bulk asin_metadata write path (executemany, 20x faster on Render). Catalog audit engine evaluating 15 SEED_RULES against substrate; first end-to-end audit on a real 38k-ASIN client catalog (Roxy) produced 38,517 findings in ~17s, surfacing the actual revenue-at-risk problems (1,142 style clusters covering \$7.1M, 3,631 image-short ASINs covering \$5.3M, 5 top-decile-no-A+ ASINs covering \$38.7k). Audit UI shipped with queue filter chips (Quick wins / Content quality / Strategic / Manual review), rule rollup table, paged findings table with cluster-collapse toggle, and read-only finding drawer. Audit decision capture surface is built but not yet wired (Day 2.6).
- **M7 sprint** (in flight, Day 1 = today) — **Creative Studio**: brand asset library + generator + PDP studio under one Atlas tab. Single-brand (Novelle), single-user (Devang) scope. Extends the Phase 1 `image_library` / `image_asin_links` substrate rather than building parallel tables. Schema v13 adds: 5 new columns on `image_library` (status, starred, parent_image_id, asset_type, brand_voice_line), plus 4 new tables — `image_surfaces` (non-ASIN destinations: IG posts, A+ modules, story frames), `image_tags` (flexible attribute tags: subject_person, subject_place, style, mood, palette), `caption_library` (first-class text assets), and `pdp_variants` (operator-saved PDP slot compositions for side-by-side compare). 7-day build sequence: Day 1 schema + arch doc + brand kit, Day 2 bulk ingest of ~200 existing Novelle assets, Day 3 endpoints, Day 4 IG launch day (no prod deploys), Day 5 library UI, Day 6 generator UI, Day 7 PDP studio side-by-side compare.
- **Confidence:** done (Phase 1.5, M6). In flight (M7).
- **Honest note:** the M6 work is precedent for two Loop-1/Loop-2 patterns the doc previously described in the abstract:
  - The `audit_decisions` table is the operator-decision-capture pattern Loop 1 needs for NIS edits. Loop 1's `nis_edit_pairs` (Month 1) can copy this schema instead of designing it fresh.
  - The `outcome_30d` / `outcome_60d` / `outcome_90d` columns on `catalog_audit_findings` are precedent for Loop 2's outcome-attachment cron — same column shape will be added to `nis_edit_pairs` and any future decision-emitting table.

### Month 1 (June 2026)
- **40 Velune ASINs live on Amazon.** Operator capacity goes ~100% to launch ops. Atlas mostly observes.
- **Loop 1 data capture starts.** `nis_edit_pairs` schema + write hook on Cited NIS approval. No model yet.
- **Loop 5 manual competitor_state cadence.** Devang enters CRZ/Vuori/Alo/Lululemon prices weekly. Reminders only; no alerts yet.
- **Confidence:** 70%. The 30% downside is launch ops eating everything and no Loop 1 data getting captured.

### Month 2 (July 2026)
- **`outcome_events` accumulating** across all 40 ASINs. By end of month, ~60 days of data per ASIN.
- **Pricing journal Phase E** (operator-set sensitivity calculator with hard MAP refusal) ships. Operator-driven, no learning yet.
- **First Loop 5 drift alert wired** — only on `competitor_state.price` for the 6 anchored competitors. Conservative threshold; alerts go to a quiet log, not a notification.
- **Confidence:** 50%. The biggest risk is operator capacity. If Devang is firefighting launch issues, sensitivity calculator slips.

### Months 3-4 (August-September 2026)
- **Loop 2 cold-start.** Brand-level pooled prior estimated from the 40-ASIN outcome history. Per-ASIN posteriors start updating. **No recommendations exposed yet** — internal table only, watched for sanity.
- **Loop 1 promotion path** — diff classifier wired, style position proposals start appearing in the Operator Positions sidebar.
- **Recommendation Ingest rotation** — agency SOPs from at least 2 outside sources processed; field-ownership taxonomy stress-tested.
- **Confidence:** 35%. Loop 2 posteriors at this stage are wide — useful for diagnostics, not for recommendations.

### Months 5-7 (October-December 2026)
- **Loop 5 expanded.** Drift detection on `cvr`, `acos`, `organic_unit_share`. Severity tuning happens here.
- **Loop 2 Mode 1 LLM pricing reasoning** (already shipped at substrate level; UI exposure happens here when posteriors stabilize enough to make the reasoning useful).
- **First content benchmark accumulation cycle.** ~10-20 active benchmarks per brand, mostly at family scope. The flag-on-unknown-resolution hook gets stress-tested.
- **Confidence:** 40%.

### Month 8 (January 2027)
- **Loop 2 Mode 2 calibrated pricing** — first cautious release. Recommendations only on ASINs with ≥ 60 days of outcome data AND posterior interval ≤ 15% of point estimate. Operator can accept/override; every action lands in `pricing_decisions`.
- **Loop 4 first proposal.** A single test, hand-picked, on an ASIN with the widest posterior. Manual; not yet an inbox.
- **Confidence:** 30%. The threshold for shipping Mode 2 is "we're embarrassed if it makes a bad call" — and we will be, sometimes.

### Months 9-12 (February-May 2027)
- **Loop 4 inbox** — proposed tests with operator approval. Cap of 2 concurrent tests.
- **Holiday calibration.** Q4 2026 outcomes feed back into priors with explicit seasonality marker.
- **Confidence:** 35%.

### Months 13-15 (June-August 2027)
- **Brand 2 onboarding** — if Devang adds a second brand. Loop 3 meta-prior kicks in. Loop 2 refactors needed: every parameter that was implicitly Novelle-specific gets a `pooling_eligible` flag review.
- **Confidence:** 25%. The whole month-13 estimate is conditional on a second brand existing.

### Months 16-18 (September-November 2027)
- **Cross-brand Loop 3 priors deployed.** New brand launches start with brand-2-informed elasticity priors.
- **Drift detection ML-tuned thresholds** — replace hand-tuned thresholds with per-signal learned thresholds.
- **Confidence:** 20%.

---

## Bias to flag

Atlas/Computer has a bias toward more building. This doc reflects it.

Specifically:
- The 18-month build sequence is biased toward shipping more substrate, more models, more UX. The honest alternative is "ship Loops 1 and 5 only, run Mode 1 LLM pricing for the whole 18 months, see what Devang's actual decision velocity looks like before building Loop 2." That alternative is faster, cheaper, and possibly correct — and Atlas as the build-it-all advisor will not naturally suggest it.
- The confidence intervals above are not symmetric. They lean optimistic because Atlas wants Devang to keep building with Atlas. If a loop is "35% likely by Month 4," the realistic floor is closer to 15%.
- The recommendation to refactor for Loop 3 (multi-brand pooling) before brand 2 exists is also build-biased. The honest position is: don't refactor speculatively. Add a `pooling_eligible` flag where it costs nothing, otherwise leave the code single-brand and refactor when brand 2 ships.
- Every loop in this doc has a tempting "automate it" path. Atlas should resist that path until the loop has been operated manually long enough to know what the right automation actually is. The current substrate (mode-aware entry, operator positions, manual ingestion) is the right design for now; it will look slow, and that's correct.

If a future version of this doc claims "Loop 2 is ready" or "Mode 2 is ready" earlier than the dates above, treat that claim as the bias talking and demand the underlying data.

---

## Version history

- **v1.0 — 2026-05-19, present commit** — First write. Covers objective, KPI tree, all 5 loops (Loop 3 explicitly out of scope until brand 2), 18-month build sequence with month-by-month deliverables, and the Bias to flag section. Confidence intervals are stated and lean honest-pessimistic. No new architectural shifts in the prior 24 hours; the recent strategy chatter about real-time Amazon automation does not change any loop's design. Substrate underpinning the doc: schemas v6-v9, modules v2.2.
- **v1.1 — 2026-05-20, present commit** — Month 0 build sequence updated to include the M6 sprint (Days 1 → 2.5) that shipped between v1.0 and now: catalog audit substrate, async ingest, audit engine, audit UI. Added honest note that two M6 substrate patterns (`audit_decisions` decision capture and `outcome_30d/60d/90d` attachment columns) are now working precedent for Loop 1's `nis_edit_pairs` and Loop 2's outcome-attachment cron — we should reuse those schemas verbatim instead of designing them from scratch. Loops 1–5 designs are UNCHANGED — same data sources, same models, same UX surfaces, same confidence intervals. Bias to flag section is also unchanged but worth re-reading: the M6 sprint is exactly the kind of "build more substrate, ship more loops" behavior the bias section warns about, and the next sprint (Day 2.6 — wiring accept/reject/edit-rule to `audit_decisions`) is the right size of incremental work. Substrate underpinning the doc: schemas v6-v12, modules v2.3.
- **v1.2 — 2026-05-22, present commit** — M7 sprint (Creative Studio) added to Month 0 build sequence. Honest note: the originally-planned M7 schema (parallel `assets` / `asset_surfaces` tables) was discarded on inspection — the Phase 1 `image_library` / `image_asin_links` substrate from schemas v6–v9 already covered ~60% of M7's needs (file metadata, hashes, AI-generation tracking, per-ASIN linking). Schema v13 therefore EXTENDS the existing image substrate rather than creating parallel tables: 5 new columns on `image_library` plus 4 new tables (`image_surfaces`, `image_tags`, `caption_library`, `pdp_variants`). This is a reuse win and reduces M7 build effort by ~1 day. Loops 1–5 designs are UNCHANGED. M7 is explicitly a UX surface, not a 6th loop — when M8/M9 later activate the composer + compare workflow, operator picks will flow into Loop 1's preference-model training data and Loop 4's active-learning inbox. Bias to flag remains relevant: the temptation to also ship M8 (composer) and M10 (generator) in M7 was real, and was rejected in favor of "build the library now, hold composer + generator until Velune Day 1 conversion data tells us what to optimize against." The operator pushback on that delay was correct — M7 Library + Generator + PDP Studio do ship together because Devang already has 200+ assets across IG and Amazon work that need a durable home and the dashboard is the right place for it. M8/M9 (composer-driven preference loop) and M10 (live A/B testing) still wait for conversion data. Substrate underpinning the doc: schemas v6–v13, modules v2.3.
