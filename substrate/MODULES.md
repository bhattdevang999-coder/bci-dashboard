# Atlas Modules — Strategy Map

> Reference for what exists, what's stub, what's deferred. Source of truth for "is X built yet?" questions. Permanent — versioned at the bottom, never rewritten in place.
> Last full audit: 2026-05-16, SHA `2b75615`

This file is for the team. When someone asks "does Atlas do X?", point them here before they go read 20,000 lines of `app.py`.

---

## Classification system

Every module is labeled with three orthogonal attributes, not one stage.

| Attribute | Values | What it means |
|---|---|---|
| **State** | `live` / `beta` / `stub` / `placeholder` | Does it work end-to-end? |
| **Substrate** | `full` / `partial` / `none` / `n/a` | Does it write to `decision_event` + operator_response + sessions? |
| **Closed-loop ready** | `yes` / `partial` / `no` | Will Phase 2 attribution be able to read its data? |

A module can be `live` but have `none` substrate (looks done, isn't substrate-native). A module can be `stub` but have `full` substrate (rare — the substrate was built first). Both situations exist in the codebase right now.

---

## Module inventory (today)

### Core platform (no `Create/Monitor/Manage/Promote` section)

| Module | State | Substrate | Closed-loop | Notes |
|---|---|---|---|---|
| **Home** (`showAtlasPage`) | live | n/a | n/a | Module switcher landing page. Not a workflow. |
| **Inputs** (`showInputsPage`) | live | full | yes | Unified dropzone + `ingestion_records` audit + freshness bar. Phase 1 Week 1. |
| **Memory** (`showMemoryPage`) | live | full | yes | Sessions sub-tab + Decisions sub-tab. Reads `substrate_events`, `substrate_sessions`. Phase 1 Week 2. |
| **Marketing** (`showMarketingPage`) | live | full | yes | Keyword library + Day-1 wizard + Budget sub-tab. Writes module=marketing and module=budget decision events. |
| **User directions** (`/docs/onboarding`) | live | n/a | n/a | Static doc with English/Urdu/Bangla toggle. |

### `01 Create` — listing creation

| Module | State | Substrate | Closed-loop | Notes |
|---|---|---|---|---|
| **Bulk Upload** (`showNISPage`) | live | **full** (as of `2b75615`) | yes | 4-step flow. Logs 8 fields × N styles per batch. Now anchors with ASIN for pre_change_snapshot capture. Visual shell predates current design system (cosmetic re-skin still pending). |
| **Image → NIS** (`showBetaImageNISPage`) | beta | none | no | Endpoints under `/api/beta-image-nis/*`. Photo-driven NIS generation. **No decision_events written today** — this is a real gap. Listed as a one-off retrofit alongside the modules strategy map. |
| **Photo Brief** | placeholder | n/a | n/a | Sidebar item routes to `showPreviewPage('photo-brief')` — coming-soon teaser, no logic. |
| **Voice Tuner** | placeholder | n/a | n/a | Same — teaser only. |

### `02 Monitor` — catalog hygiene

| Module | State | Substrate | Closed-loop | Notes |
|---|---|---|---|---|
| **Catalog Health** (`showCatalogPage`) | live | partial | partial | Substrate writes happen, but as rolled-up findings (one event per issue category, not per ASIN). Closed-loop can read aggregate severity but not per-ASIN attribution. Acceptable for v1 — would need refactor to go full per-ASIN. |
| **Suppression Watch** | placeholder | n/a | n/a | Teaser. |
| **Image Audit** | placeholder | n/a | n/a | Teaser. |
| **Compliance** | placeholder | n/a | n/a | Teaser. |

### `03 Manage` — listing operations

| Module | State | Substrate | Closed-loop | Notes |
|---|---|---|---|---|
| **Bulk Price Sync** | placeholder | n/a | n/a | Teaser. |
| **Content Refresh** | placeholder | n/a | n/a | Teaser. |
| **Variations** (`showMergePage`) | live | **full** | yes | Endpoints under `/api/merge/*`. Parent/child reconciliation. `/analyze` opens a Variations session and logs one decision_event per proposed action (`field_name='parentage_correction'`). `/approve` writes an accept/reject operator_response linked to the matching event. Module enum: `Module.VARIATIONS`. |
| **A/B Test** | placeholder | n/a | n/a | Teaser. |

### `04 Promote` — ads + traffic

| Module | State | Substrate | Closed-loop | Notes |
|---|---|---|---|---|
| **Ad Bulksheet** | placeholder | n/a | n/a | Teaser. The "real" PPC build now lives under Marketing → Wizard → batch export. This sidebar item is a leftover. **Recommend: delete or rename.** |
| **Keywords** | placeholder | n/a | n/a | Teaser. Same caveat — the real one lives under Marketing. |
| **PDP Optimizer** | placeholder | n/a | n/a | Teaser. |
| **Reviews** | placeholder | n/a | n/a | Teaser. Earlier proof-of-concept (`novelle-reviews`) sat in a tiiny artifact, not in app. |
| **Recommendations** (`showIntelPage`) | live (hidden) | **none** | no | Endpoint `/api/intel/accept` exists. Sidebar entry is `style="display:none;"` so it's not visible. Not used in current flow. |

### `∇ Lab` — experimental

| Module | State | Substrate | Closed-loop | Notes |
|---|---|---|---|---|
| **Listing Lab** (`showLabGridPage`) | beta | **none** | no | Endpoints under `/api/lab/*`. Grid-based NIS variant generation. No substrate writes. Lower priority — this is an internal R&D surface, not an operator workflow. |

### `Admin`

| Module | State | Substrate | Closed-loop | Notes |
|---|---|---|---|---|
| **Inspect Rules** (`showEnginePage`) | live | n/a | n/a | Read-only inspection of the NIS rule engine. No writes. |

---

## Substrate write call-sites (the whole list)

Counted from `app.py` plus the `substrate/budget.py` self-write. Three call sites in app.py + one in budget = four total places where `log_field_decision` runs:

| File · line | Module written | When it fires |
|---|---|---|
| `app.py:3676` | `Module.NIS` | Each style during `/api/generate-content` |
| `app.py:9596` | `Module.CATALOG_HEALTH` | Per issue category after catalog analysis |
| `app.py:11742` | `Module.MARKETING` | Per keyword candidate proposed by Day-1 wizard |
| `substrate/budget.py:152` | `Module.BUDGET` | Per `set_budget` call (audit trail for monthly allocations) |
| `app.py:10095` | `Module.VARIATIONS` | Per merge-plan action proposed by `/api/merge/analyze` |
| `app.py:10208` | `Module.VARIATIONS` (response) | Per `/api/merge/approve` accept/reject |
| `substrate/brand_voice.py:save_voice` | `Module.BRAND_VOICE` | Per `POST /api/atlas/brand-voice` save (auto-bumps `profile_version`) |
| `app.py:12138` | `Module.NIS` (image) | Per generated field in `/api/beta-image-nis/generate` |
| `app.py:12248` | `Module.NIS` (response) | Per `field_wrong` feedback in `/api/beta-image-nis/feedback` |
| `substrate/unit_economics.py:record_sales_observations` | `Module.UNIT_ECONOMICS` (via outcome_events) | Per `/api/catalog/upload-sales` upload — one outcome_events row per (ASIN, metric) per period |
| `substrate/cost_inputs.py:save_cost_input` | `Module.UNIT_ECONOMICS` | Per `POST /api/atlas/unit-economics/costs/<asin>` (operator-supplied landed/FBA/3PL/MAP) |
| `substrate/cost_inputs.py:save_overhead` | `Module.UNIT_ECONOMICS` | Per `POST /api/atlas/unit-economics/overhead` (brand fixed overhead) |

Module enum values reserved but **not yet used**: `EXPERIMENTS`, `LEAK`, `OTHER`. They exist in `schema.py` so adding them later requires no migration.

---

## Real gaps (not opinions — actual missing wiring)

These are the modules that present an operator UI but write **no** substrate. Anything done in them today is invisible to Memory and unrecoverable for Phase 2 attribution.

| Module | What's lost | Estimated retrofit cost |
|---|---|---|
| ~~**Image → NIS (beta)**~~ | ~~Every photo-driven NIS generation~~ | ~~Small~~ | **CLOSED at `eb32657`** — retrofitted. |
| ~~**Variations**~~ | ~~Every parent/child merge or split decision~~ | ~~Small~~ | **CLOSED at the present commit** — retrofitted. |
| **Listing Lab** | Every variant generation attempt + which variant the operator picked | Small but lower priority — R&D tool, not a daily workflow. |
| **Recommendations** | Endpoint `/api/intel/accept` exists but the UI is hidden. If we re-enable it, substrate writes need to be added at the same time. | Small. Don't re-enable the nav item until this is done. |

---

## Sequencing — what to build, in what order

Strict priority. Numbered so we can reference these as "Item 1", "Item 2".

1. **Phase 2 closed-loop attribution** — blocked on data, not code. Resume in 4-6 weeks when there are enough before/after pairs to attribute against. Until then, the Confound view (shipped at `ea54e04`) is the honest interim.
2. ~~**Image → NIS substrate retrofit**~~ — **DONE at `eb32657`.**
3. ~~**Variations substrate retrofit**~~ — **DONE at the present commit.**
4. **NIS page visual re-skin** — cosmetic. The Bulk Upload page works fine, it just doesn't share the Memory/Marketing/Budget design system. Standalone polish pass.
5. **Catalog Health → per-ASIN substrate** — refactor from category roll-ups to per-ASIN decision_events. Only do this once Phase 2 needs per-ASIN granularity. Otherwise current aggregation is fine.
6. ~~**Sidebar cleanup**~~ — **Reconsidered and done differently.** On closer audit, the `Promote → Ad Bulksheet` and `Promote → Keywords` preview pages aren't duplicates of Marketing — they describe a richer future state (catalog-aware auto-sync, Brand-Analytics-refreshed unified keyword DB, cannibalization detection) that Marketing today only partially covers. Deleting them would erase legitimate roadmap signaling. Resolved by adding a "Today, under Marketing" callout to both preview pages that links to the live Marketing page — surfaces the relationship without destroying future-state content.
7. **Listing Lab substrate retrofit** — lowest priority remaining gap. R&D tool, not daily workflow.

---

## What's deliberately NOT being built (and why)

These came up as ideas, were considered, and declined. Listed here so they don't get rebuilt by accident.

| Idea | Why it was declined |
|---|---|
| **Operational cost tracking in Budget** | Strictly PPC for v1. Operational costs (photography, A+ design, content rewrites) muddy the attribution signal and the operator's planning cadence is different for them. Will reconsider after 3 months of Budget usage. |
| **Sub-monthly budget granularity** | PPC has multi-day attribution tails; daily/weekly variance is statistical noise on small budgets. Month is the right grain for now. |
| **Per-ASIN Budget UI** | Substrate already supports `scope_type='asin'` — UI just doesn't expose it. Add only when an operator demand is real, not preemptively. |
| **Image library / photo memory** | Deferred from Phase 1 by an explicit decision. The substrate has `image_library` and `image_asin_links` tables ready, but no UI and no Phase 1 commitment to build one. Defer to Phase 2 prep. |
| **Forecasting in Budget variance** | Variance compares actual to planned. We don't predict future actual from past data here — that's an analytics layer concern, not a substrate concern. |
| **Forecasting in Memory tab** | Same principle. Memory is the audit trail. Predictions belong in a separate analytics surface. |
| **Operator-feedback summarization via LLM** | The session-submit modal already captures structured 3/5/7 questions. Don't add an LLM summary layer until we actually have months of session data to summarize. |

---

## Glossary — terms used in the codebase

This list isn't redundant — `schema.py` defines the data shapes, but the team uses these words in conversation and the meanings drift if they're not pinned down.

| Term | Definition |
|---|---|
| **Atlas module** | A workflow surface in the sidebar (NIS Bulk Upload, Catalog Health, Marketing, etc.). One module typically owns one or more endpoints + one or more UI tabs. |
| **Substrate** | The locked Postgres tables (`substrate_events`, `substrate_sessions`, `outcome_events`, `ingestion_records`, plus module-specific projections like `keyword_library` and `budget`). |
| **decision_event** | The atom. One per operator-touching call Atlas makes. Carries `module`, `field_name`, `atlas_output`, `rules_injected`, `overall_confidence`, optional `pre_change_snapshot`. |
| **operator_response** | The reply to a decision_event. `accept` / `edit` / `reject` / `view` / `add_comment`, with `scope` `none / just_this / batch / brand_always`. |
| **session** | A grouped batch of decision_events from one operator sitting. NIS uploads, marketing wizard runs, catalog analyses all open sessions. |
| **judgment_moment_event** | Detector output. Fires for `low_confidence`, `rule_override`, `in_session_pattern`. Surfaced as toasts during the flow; collected for Memory. |
| **pre_change_snapshot** | The frozen before-state of an ASIN at decision time. The architecturally-irreversible piece. Empty when no outcome data exists for the ASIN. |
| **STRATEGIC_FIELDS** | Set of field names that bypass the confidence/rule-density log filter. All 8 NIS user-visible fields are in this set. |
| **scope** (operator action) | How the operator wants the action applied: `just_this` / `batch` / `brand_always` / `propose_rule`. Affects rule library promotion. |
| **scope** (budget) | Which dimension a budget row covers: `overall` / `theme` / `asin`. |
| **theme** (marketing/budget) | A keyword's strategic intent: `branded` / `feature` / `competitor`. Resolved at variance time via LATERAL join from `outcome_events.keyword` to the most-recent marketing decision_event proposing it. |
| **workspace** | A brand. Single-tenant single-brand mode in v1 means it's always `novelle` (or whatever `ATLAS_VISIBLE_BRANDS` is set to). |

---

## Version history

Append below this line. Do not edit entries above.

- **v1.0 — 2026-05-16, SHA `2b75615`** — Initial strategy map. Audit conducted after the NIS substrate retrofit (pass ASIN to `log_field_decision`). 4 substrate-writing modules confirmed (NIS, Catalog Health, Marketing, Budget). 3 real gaps identified (Image → NIS, Variations, Listing Lab). 6-item sequencing list. Glossary pinned down.
- **v1.1 — 2026-05-16, SHA `eb32657`** — Image → NIS retrofit shipped. Now substrate-native: opens session per `/generate`, logs 8 decision_events with `nis.image.vision_driven` rule marker, attaches operator_responses on `field_wrong` feedback. Gap closed.
- **v1.2 — 2026-05-16, SHA `8efc3f6`** — Variations retrofit shipped. `Module.VARIATIONS` enum added. `/api/merge/analyze` opens a session and logs one decision_event per proposed action (`field_name='parentage_correction'`). `/api/merge/approve` writes accept/reject operator_responses. Now 6 substrate-writing modules + Confound view. Variations + Image → NIS no longer in the gap list. Three real gaps remain: Listing Lab (lowest priority), hidden Recommendations endpoint, and Catalog Health per-ASIN granularity (blocked-on-need).
- **v1.3 — 2026-05-16, present commit** — Sidebar cleanup reconsidered. Preview pages for Ad Bulksheet + Keywords are NOT duplicates of Marketing; they describe a richer future state Marketing only partially covers today. Resolved by adding `today_in_marketing` callout to both preview pages instead of deleting them. Item #6 in sequencing crossed off and rewritten.
- **v1.4 — 2026-05-16, present commit** — Brand Voice module shipped (Step 2 of BRAND_VOICE.md). New sidebar item, new substrate module, new `Module.BRAND_VOICE` enum value. NIS prompt now consumes structured voice (tone descriptors, hero adjectives, signature phrases, banned phrasings, like/unlike examples). Profile versions auto-bump on every save; audit trail finally functional. Two-store split closed for voice (operational defaults stay in JSON; all voice in `brand_profile`).
- **v1.5 — 2026-05-16, prior commit** — Unit Economics Phase B shipped (`UNIT_ECONOMICS.md` v1.1). `Module.UNIT_ECONOMICS` enum added; `substrate/unit_economics.py:record_sales_observations` writes one outcome_events row per (ASIN, metric) per period; `/api/catalog/upload-sales` wired to call it (best-effort, never blocks). Nine sales metrics now durable: `revenue`, `units_sold`, `sessions`, `returns`, `return_rate`, `cvr`, `buy_box_pct`, `ad_spend`, `ad_revenue`, `acos`. Sales data is no longer trapped in `catalog_health_state` in-memory — it lives in the substrate, reachable by Memory and `pre_change_snapshot`. Empty cells skipped (not zero-filled); no-ASIN rows skipped. Phases C–F (cost inputs, margin rollup, sensitivity calculator, price-range observations) remain blocked on operator confirmation of UNIT_ECONOMICS.md§"Open questions" and on operator-supplied cost data.
- **v1.6 — 2026-05-16, prior commit** — Unit Economics Phase C + D shipped (`UNIT_ECONOMICS.md` v1.2). Two new tables (`cost_inputs`, `brand_overhead`); schema bumped to v5. `substrate/cost_inputs.py` writes per-ASIN cost rows + brand-level overhead; every save lands a `decision_event` with `module='unit_economics'`. `substrate/margin.py:margin_rollup` joins `cost_inputs` + `outcome_events` and produces three margin columns side by side: contribution / TACOS / net-after-ads. Missing-cost rows surface contribution as `None`, never `$0`. MAP warning fires when avg selling price < MAP. New sidebar item `Unit Economics` under Core with three tabs (Margin rollup / Cost inputs / Brand overhead). Five new `/api/atlas/unit-economics/*` endpoints. **Phase E** (sensitivity calculator with hard MAP refusal) and **Phase F** (price-range observations) remain.
- **v1.7 — 2026-05-18, present commit** — Phase 1.5 architecture locked across 10 design docs (M1-M5 sprint, single-operator, manual-first). New docs: `CONTEXT.md` (L0 context injection, schema migration v6 adds 4 provenance columns to substrate_events), `CITATION_CHAIN.md` (5-layer reasoning on every NIS output, citation verifier, citation_rejections table), `UNKNOWNS.md` (ignorance catalog primitive, owner-routed queues, decision_class_requirements config), `ASIN_METADATA.md` (ground truth per ASIN with parent-child inheritance, source attribution + operator confirmation), `OPERATOR_POSITIONS.md` (one-operator-per-brand locked, position promotion from edits), `BRAND_POSITION.md` (Velune position locked: CRZ-adjacent at $35–55, 6-competitor frame, quarterly review), `PRICING_LOGIC.md` (operator-set floor/ceiling rules, Mode 1 LLM reasoning Day 1, Mode 2 calibrated Month 6+), `RECOMMENDATION_INGEST.md` (agency inbox + tokenized response link, no agency login, field ownership taxonomy), `CONTENT_BENCHMARKS.md` (reusable patterns, scope hierarchy, auto-flag on unknown resolution), `MANUAL_INGESTION.md` (manual-first data path, SP-API as future write-path, source-attribution-and-reconciliation rules). Build sequence corrected: M1+M3 paired (Days 1-3, every milestone has visible UX). Code starts tomorrow.
- **v1.8 — 2026-05-18, present commit** — M1+M3 shipped: schema v6 (provenance cols + unknowns + citation_rejections), substrate/context.py (L0 9-layer assembly), substrate/unknowns.py (emit/list/resolve/dedupe), substrate/citation_chain.py (5-layer cited generation, lenient verifier). New endpoints: /api/atlas/cited-nis/generate, /api/atlas/cited-nis/preview-context, /api/atlas/unknowns (list + resolve), /api/atlas/citation-rejections. New sidebar items: Cited NIS, Unknowns. 41 new QA assertions across qa_context_layer + qa_citation_chain; 15 QA suites total green. Existing NIS pipeline untouched. M2 (asin_metadata + operator_positions onboarding) is next.
- **v1.9 — 2026-05-18 (later), present commit** — M2 shipped: schema v7 + six new substrate primitives + mode-aware UI. Schema v7 adds `asin_metadata`, `brand_position`, `operator_positions`, `pricing_logic`, `pricing_decisions`, `competitor_state`. New writers: `substrate/asin_metadata.py` (parent→child inheritance resolved at read time; variation-axis fields child-only; `confirm_field` + `record_field_source` for the operator-confirmation flow), `substrate/operator_positions.py` (scope-priority read; hard_refusals bubble first regardless of scope; create/archive/supersede), `substrate/brand_position.py` (one row per workspace, revision bumps on update, `update_review_timestamp` for reaffirm without revision bump), `substrate/pricing_logic.py` (scope=global/family/asin; `compute_floor_from_rule` for variable_contribution_zero; `pricing_decisions` journal with 30/60/90d outcome attachment), `substrate/competitor_state.py` (manual observations of price/review_count/bsr/listing_changed). New `substrate/field_suggest.py` reads `substrate/field_schema.yml` and resolves the four entry modes: `substrate_read` / `q_and_a` / `llm_suggest` / `manual_only`. New endpoints under `/api/atlas/`: field-schema, field-suggest, asin-metadata (CRUD + confirm-field + per-field source), asin-metadata/family/<parent>, brand-position, operator-positions (CRUD + archive), pricing-logic, pricing-decisions, pricing-floor/compute, competitor-state, velune-onboarding (2 parents → 40 children + 5 starter positions). New sidebar items: Brand Position, Operator Positions, Pricing, Velune Onboarding. `qa_m2.py` ships 70 assertions; 15 prior QA suites still green; 32/32 substrate unit tests green. M4 (recommendation ingest + content benchmarks) is next.
- **v2.0 — 2026-05-18 (latest), present commit** — M4 shipped: schema v8 + recommendation ingest + tokenized agency response. Schema v8 adds `recommendation_ingest` (with single-use response_token + expiry) and `atlas_evaluation` (per-field verdict + agency response + operator decision + final_value). New writers: `substrate/recommendation_ingest.py` (CRUD + `generate_response_token` + `lookup_by_token` with expiry check + `mark_response_received` + `consume_token`), `substrate/atlas_evaluation.py` (per-field verdicts with field-owner taxonomy [manufacturer/agency/amazon_taxonomy/operator_strategic/atlas_calibrated/ambiguous], `apply_agency_response` for tokenized writeback, `apply_operator_decision` for accept/override/defer/reject, `summarize_rec` for verdict counts). `substrate/rec_evaluator.py` runs the two LLM passes (parse_raw_text + evaluate_recommendation) with heuristic fallbacks when the LLM is unavailable — degraded path produces 'unknown' verdicts routed by heuristic owner, so ingest never blocks. New endpoints under `/api/atlas/`: recommendations (list/create/get/evaluate/token/status), evaluations/<id>/operator-decision. New PUBLIC endpoints (no login): GET /respond/<rec_id>?token=..., POST /respond/<rec_id>/draft, POST /respond/<rec_id>/submit. New template `_atlas_respond.html` is the agency-facing form — read-only context, per-field response textarea + 1-5 confidence radio, save draft / final submit. New sidebar item: Recommendations (inbox + ingest form + detail view with per-field verdict table, generate-response-link button, operator decision UI). `qa_m4.py` ships 63 assertions; 15 prior tmp suites + qa_m2.py still green; 32/32 substrate unit tests green. End-to-end test (test_client) walks the full flow: ingest → evaluate → token gen → public form renders → bad-token rejects → valid submit writes agency_response → token consumed → reuse blocked. M5 (content_benchmarks) is next.
