# MANUAL_INGESTION.md — Manual-First Data Path

**Status:** design (pre-build)
**Author:** Devang / Atlas
**Date:** 2026-05-18
**Milestone:** M1+M2 (manual upload surfaces ship in days 1-5)

---

## Purpose

The 10-day sprint is built around **manual data ingestion**.
SP-API auth (and other API integrations like Helium 10 / Keepa)
land later as additional write-paths into the same substrate
tables, not as the architectural foundation.

This is the operator's call from earlier today: "Let's build
this independent of Amazon access through API first... let's
manually feed data so that when api happens we can see what
other data is accessible and what else can we do."

The decision is correct. APIs go down, schemas change, auth
expires. A system designed around manual ingestion with API as
an optimization is more durable than one that assumes API
access.

---

## Source field on every ingestable table

Every table that accepts data writes carries a `source` column:

```
SOURCE                       MEANING
─────────────────────────────────────────────────────────────
manual_upload                operator pasted/uploaded a file
operator_typed               operator typed directly in form
agency_provided              came from agency-supplied doc
factory_provided             came from factory spec sheet
sp_api                       Amazon SP-API (future write-path)
helium10                     Helium 10 export (future)
keepa                        Keepa API (future)
jungle_scout                 Jungle Scout API (future)
amazon_scrape                fragile, against ToS — not used
parsed_pdf                   LLM-parsed from PDF
```

Tables with a `source` field (M1-M5 scope):
- `outcome_events` (Phase B already shipped — value via business
  reports manually uploaded)
- `cost_inputs` (already operator-supplied)
- `asin_metadata.field_sources` (per-field source attribution,
  see ASIN_METADATA.md)
- `competitor_state` (NEW — operator types CRZ price weekly)
- `recommendation_ingest` (source = the agency/vendor name)

---

## Reconciliation rule for when APIs arrive

When SP-API or Helium 10 lands later, the same substrate tables
get additional write-paths. Conflict resolution:

```
preferred_source priority (per metric):

OUTCOME_EVENTS (sales, sessions, revenue, returns)
  1. sp_api          (most authoritative when present)
  2. helium10        (rich but lagged 24-48h)
  3. manual_upload   (fallback)
  4. operator_typed  (rare; usually correction)

COMPETITOR_STATE (CRZ price, etc.)
  1. helium10        (auto-pulled if subscription exists)
  2. jungle_scout    (alternate)
  3. operator_typed  (manual entry)

ASIN_METADATA (factory facts)
  1. factory_provided (most authoritative)
  2. operator_typed   (operator confirms or overrides)
  3. agency_provided  (lower trust, requires confirmation)
  4. parsed_pdf       (lowest trust, requires confirmation)
```

When two sources disagree on the same metric for the same
period:
- Higher-priority source wins by default
- Discrepancy is logged in `substrate_events` with both values
- Operator gets a "data discrepancy" notification surfaced in
  the Unknowns view

Discrepancies are signal, not noise. They reveal data quality
problems and mismatched assumptions.

---

## Manual upload surfaces (M1-M2 scope)

```
SURFACE                    SHIPS IN     ACCEPTS
─────────────────────────────────────────────────────────────
Sales Upload               M1 (Day 1)   Amazon business
                                        report (CSV/XLSX)

Catalog Upload             already      Amazon catalog export
                           shipped       (CSV)

Cost Inputs                already      operator types per
                           shipped       ASIN

Backend Doc                M2 (Day 4)   agency PDF or pasted
                                         text → asin_metadata

Brand Voice                already      operator types
                           shipped

Operator Position           M2 (Day 4)   operator types or
                                         promoted from edit

Brand Position              M2 (Day 4)   operator types

Pricing Logic               M2 (Day 4)   operator types floor/
                                         ceiling rules

Competitor Snapshot         M2 (Day 5)   operator types CRZ
                                         price weekly

Recommendation              M4 (Day 6-8) operator pastes/uploads
                                         agency doc or vendor
                                         email
```

Each upload surface validates inputs at write time:
- Required fields enforced
- Type coercion (dollar signs stripped, percentages normalized)
- Source field set automatically based on entry path
- Operator confirmation required before substrate writes

---

## SP-API write-path arrives later

When SP-API auth lands (after the 10-day sprint), the integration
adds:

```
1. Daily sales pull
   GET_SALES_AND_TRAFFIC_REPORT → outcome_events
   Source field: 'sp_api'
   Frequency: daily cron
   Replaces operator-uploaded business reports as preferred
   source for sales/sessions/revenue.

2. Listing detail pull
   GetListingsItem → asin_metadata
   Source field: 'sp_api'
   Frequency: weekly cron
   Reconciles against operator-confirmed asin_metadata; flags
   discrepancies (e.g., Amazon's record differs from our
   ground truth).

3. Inventory pull
   GET_FBA_INVENTORY_AVAILABILITY_DATA → asin_state
   Source field: 'sp_api'
   Frequency: daily cron
   Powers inventory-aware throttle (stockout prevention).

4. Order/return data
   GET_RETURNS_DATA → outcome_events
   Source field: 'sp_api'
   Reconciles against operator-uploaded return data.
```

Each of these is a new write-path. None of them require
substrate redesign — they write to existing tables with
source='sp_api'. The reconciliation rule above governs
conflicts.

---

## What this does NOT do (scope guard)

- Does NOT block on SP-API auth. The full system functions
  with manual ingestion only. SP-API is acceleration, not
  prerequisite.
- Does NOT auto-fetch from Helium 10 / Keepa / Jungle Scout
  in the 10-day sprint. Those are subsequent write-paths.
- Does NOT scrape Amazon. Against ToS, fragile, never the
  source of substrate data.
- Does NOT polish the manual upload UX in v1. The first
  version is functional, not delightful. Operator efficiency
  improves in week 3+ based on actual usage patterns.

---

## Failure modes

1. **Operator forgets to upload**: business report arrives
   monthly but operator delays the upload for a week. Stale
   data downstream. Mitigation: dashboard shows
   "last_outcome_event_at" prominently; alerts when stale
   beyond expected cadence.

2. **Manual entry typos**: operator types $32.5 when meant
   $325. Mitigation: per-field range validators (price
   plausibility check against pricing_logic; cost
   plausibility check against margin computation). Outliers
   flagged before substrate write.

3. **Source attribution drift**: operator pastes data without
   noting source; system tags as 'operator_typed' but value
   actually came from agency. Future API pull may overwrite
   it incorrectly per priority rule. Mitigation: operator
   always picks source on entry; default is workflow-
   appropriate (e.g., backend doc upload defaults to
   'agency_provided').

---

## Version history

Append below this line.

- **v1.0 — 2026-05-18, present commit** — Initial design.
  Manual-first data path locked. SP-API and other API write-
  paths land later as optimizations. Source field on every
  ingestable table. Reconciliation priority rule per metric
  type. Discrepancy logging surfaces in Unknowns view.
  10-day sprint operates entirely without API auth.
