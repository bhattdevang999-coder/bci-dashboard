# RECOMMENDATION_INGEST.md — Agency Inbox + Tokenized Response Link

**Status:** design (pre-build)
**Author:** Devang / Atlas
**Date:** 2026-05-18
**Milestone:** M4 (Days 6–8)

---

## Purpose

Every external recommendation — agency, vendor, SOP, internal
note — lands in substrate as first-class data. Atlas evaluates
each field against the 5-layer reasoning chain, surfaces
verdicts with citations, routes unknowns to the right owner
queue, and (when the source is an agency) generates a tokenized
response link that lets the agency answer questions inside the
system without needing dashboard login.

This is the substrate equivalent of the principle the operator
named: "Why don't we have agency use dashboard to answer
questions? Because technically we're making it available
there."

---

## Schema

```sql
CREATE TABLE IF NOT EXISTS recommendation_ingest (
  rec_id              TEXT PRIMARY KEY,
  workspace_id        TEXT NOT NULL,

  source              TEXT NOT NULL,
                      -- 'acme_agency' | 'helium10' | 'jungle_scout'
                      -- | 'operator_note' | 'sop_doc' | etc.
  source_tier         TEXT,
                      -- 'top_agency' | 'mid_agency' | 'budget_agency'
                      -- | 'vendor_tool' | 'operator' | 'internal_sop'
  source_contact      TEXT,
                      -- e.g., agency contact email, for tokenized
                      -- response link delivery

  raw_text            TEXT,
                      -- original verbatim text or extracted from PDF
  raw_file_path       TEXT,
                      -- workspace path to the original file
  raw_file_hash       TEXT,
                      -- dedup against re-uploads

  parsed_fields       JSONB,
                      -- structured extraction from raw_text;
                      -- one entry per field-value pair
  scope_asins         TEXT[],
                      -- which ASINs this rec applies to
  scope_confidence    NUMERIC(4, 3),
                      -- 0-1 confidence on scope inference
  rec_type            TEXT,
                      -- 'backend_fields' | 'keyword_list'
                      -- | 'pricing_proposal' | 'listing_copy'
                      -- | 'image_brief' | 'pricing_review' | etc.

  ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ingested_by         TEXT,

  status              TEXT NOT NULL DEFAULT 'pending_evaluation',
                      -- 'pending_evaluation' | 'evaluated'
                      -- | 'awaiting_response' | 'response_received'
                      -- | 'resolved' | 'archived'

  -- Tokenized response link
  response_token      TEXT,
                      -- single-use token; nullable until generated
  response_token_url  TEXT,
                      -- full URL operator forwards to source
  response_expires_at TIMESTAMPTZ,
                      -- 7 days default
  response_received_at TIMESTAMPTZ,

  meta                JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_rec_ingest_status
  ON recommendation_ingest (workspace_id, status, ingested_at DESC);

CREATE INDEX IF NOT EXISTS idx_rec_ingest_source
  ON recommendation_ingest (workspace_id, source, ingested_at DESC);


CREATE TABLE IF NOT EXISTS atlas_evaluation (
  eval_id             TEXT PRIMARY KEY,
  rec_id              TEXT NOT NULL REFERENCES recommendation_ingest(rec_id),

  field_name          TEXT NOT NULL,
  submitted_value     TEXT,
  field_owner         TEXT NOT NULL,
                      -- 'manufacturer' | 'agency' | 'amazon_taxonomy'
                      -- | 'operator_strategic' | 'atlas_calibrated'
                      -- | 'ambiguous'

  verdict             TEXT NOT NULL,
                      -- 'agree' | 'partial' | 'disagree' | 'unknown'
  reasoning           TEXT NOT NULL,
                      -- explicit reasoning citing substrate row IDs

  citations           JSONB,
                      -- 5-layer citation chain (same format as
                      --  CITATION_CHAIN.md output)

  proposed_alternative TEXT,
                      -- when verdict='disagree' or 'partial'
  test_design         TEXT,
                      -- when verdict='partial' and testable
  evidence_path       TEXT,
                      -- when verdict='unknown'; routes to queue

  confidence          NUMERIC(4, 3),
  criticality         TEXT,
                      -- 'launch_blocking' | 'high' | 'normal' | 'low'

  agency_response     TEXT,
                      -- populated when agency answers via tokenized link
  agency_response_at  TIMESTAMPTZ,
  operator_decision   TEXT,
                      -- 'accept' | 'override' | 'defer' | 'reject'
  operator_decided_at TIMESTAMPTZ,
  final_value         TEXT,
                      -- the value that actually got applied

  evaluated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  meta                JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_atlas_eval_rec
  ON atlas_evaluation (rec_id, evaluated_at);

CREATE INDEX IF NOT EXISTS idx_atlas_eval_pending
  ON atlas_evaluation (rec_id)
  WHERE operator_decision IS NULL;
```

---

## Field ownership taxonomy

Per the operator's principle that fields have owners (not just
values), every parsed field is tagged with `field_owner`:

```
manufacturer        — physical product fact (material, GSM, UPF,
                      lining, country of origin, care)
                      → routes to Factory Questions queue

agency              — agency keyword/positioning choice (Part
                      Number, Lifestyle, Theme, listing copy)
                      → agency answers via tokenized link

amazon_taxonomy     — constrained by Amazon dropdown options
                      (Sport Type, Theme, Pattern)
                      → operator confirms what's available

operator_strategic  — operator-only decision (price, positioning,
                      voice)
                      → routes to Your Decisions queue

atlas_calibrated    — Atlas decides based on substrate (e.g.,
                      "should we test this?" — Atlas verdicts
                      these from outcome history)

ambiguous           — owner unclear; needs operator routing
                      → routes to Triage queue
```

The dashboard's Unknowns view filters by owner-assigned queue.
Operator only sees what's actually theirs to answer.

---

## Tokenized response link

For agency-owned fields (or any source where you want the
external party to respond inside the system without login):

```
Generate link flow:
  1. From recommendation_ingest row, click "Generate response link"
  2. System creates response_token (UUID, single-use)
  3. System builds response_token_url:
     https://novelle-atlas/respond/{rec_id}?token={response_token}
  4. response_expires_at = NOW() + 7 days (configurable)
  5. Operator forwards URL to agency contact (email or chat)

What the agency sees on click:
  - read-only context (brand position summary, ASIN family,
    scope statement) — see UI mockup below
  - structured form for each field where Atlas's verdict needs
    their reasoning or where their answer is required
  - submit button writes back to atlas_evaluation rows

What the agency does NOT see:
  - operator_positions
  - voice block details
  - Atlas's calibration on them
  - other ASINs not in scope
  - reasoning chain on Atlas's verdicts (only the question +
    Novelle's flag, not full audit trail)
```

The token is single-use. Once submitted, the link expires.
Operator can regenerate if agency needs to make corrections,
but each regeneration is a new token logged separately.

---

## Tokenized response page (what agency sees)

```
NOVELLE × ACME AGENCY
Velune Backend Details Review — May 18, 2026
This link expires May 25, 2026.

Background context (read-only):
  Velune is launching as two parent listings: pocket
  family + no-pocket family. 4 colors × 5 sizes per
  family. Brand positioning: CRZ-adjacent visual quality
  at $35–55.

Section A — Manufacturer facts (Novelle providing, FYI)
  [Collapsed; "14 facts being supplied by Novelle"]

Section B — Your responses needed (10 fields)

  Field: Pocket Description
  Submitted value: "No Pocket"
  Novelle's flag: Conflicts with Product Name "with Pockets"

  ☐ Agreed — revise to: ____________
  ☐ Disagree, reasoning: ____________
  ☐ Need more from Novelle: ____________

  Confidence (1-5):  ☐ ☐ ☐ ☐ ☐

  ─────────────────────────────────────

  Field: Part Number
  Submitted value: "Yoga Leggings"
  Novelle's flag: Part Number is not indexed for customer
                  search; using a keyword here doesn't help
                  ranking. Internal SKU expected.
  Novelle wants: your reasoning behind the keyword entry.

  Your response:
    ☐ Agreed — revise to: ____________
    ☐ Disagree, reasoning: ____________

  Confidence (1-5):  ☐ ☐ ☐ ☐ ☐

  ... (8 more)

Section C — Joint items (3)
  ...

[ Save draft ]  [ Submit final ]
```

Submission writes responses back to `atlas_evaluation`,
flips `recommendation_ingest.status` to 'response_received',
and notifies operator.

---

## Evaluation prompt (for Atlas's verdict pass)

```
You are evaluating an external recommendation against
Novelle's substrate.

═════════════════════════════════════════════════════════════════
CONTEXT
═════════════════════════════════════════════════════════════════
[L0 bundle: 5 layers as for content gen, plus full
 calibration_state for the source named in the ingest row]

═════════════════════════════════════════════════════════════════
INCOMING RECOMMENDATION
═════════════════════════════════════════════════════════════════
Source:        Acme Agency
Source tier:   mid-tier agency
Submitted:     2026-05-18
Source calibration on this rec_type:
                3/8 historical accuracy = 0.38

PARSED FIELDS:
  [one per field, with submitted_value]

SCOPE:
  Operator-tagged or LLM-inferred scope, with confidence.

═════════════════════════════════════════════════════════════════
YOUR JOB
═════════════════════════════════════════════════════════════════

For EACH field, output a verdict object with:
  - field_name, submitted_value
  - field_owner (per taxonomy above)
  - verdict: agree | partial | disagree | unknown
  - reasoning: 1-3 sentences citing substrate row IDs
  - citations: 5-layer chain like NIS
  - confidence: 0.0-1.0
  - criticality: launch_blocking | high | normal | low

For 'disagree' or 'partial' — also propose alternative
+ test_design (if testable).

For 'unknown' — also evidence_path + question routed to
the right owner queue.

Identify cross-field conflicts at end.

OUTPUT: strict JSON.
```

---

## Ingest pipeline

```
STEP 1   Operator pastes/uploads recommendation
         (email forward, PDF, text paste)

STEP 2   System parses raw_text → parsed_fields (LLM extraction)
         Tags scope (which ASINs apply), source, source_tier
         Writes recommendation_ingest row

STEP 3   System runs evaluation prompt (above)
         Writes atlas_evaluation row per field
         Status flips: pending_evaluation → evaluated

STEP 4   Dashboard shows evaluated recommendations
         Operator decides per field: accept/override/defer/reject
         For agency-owned fields needing response:
           generate tokenized link, send to agency

STEP 5   Agency responds via tokenized link
         atlas_evaluation rows get agency_response, agency_response_at
         Status flips: awaiting_response → response_received

STEP 6   Operator reviews agency responses, makes final decisions
         atlas_evaluation rows get operator_decision, final_value

STEP 7   Outcomes attach later (when applicable)
         Calibration_state updates per (source, rec_type) based
         on outcome quality of this recommendation set
```

---

## What this does NOT do (scope guard)

- Does NOT auto-publish agency-suggested values. Operator
  decision required on every field.
- Does NOT support multi-round agency negotiation in v1. One
  link, one response, one operator decision per ingest.
  Multi-round happens via new ingest with prior context.
- Does NOT cross-link recommendations from different sources
  on the same ASIN automatically. Operator can manually link.
- Does NOT block recommendation ingestion when scope is
  ambiguous. Atlas asks operator to disambiguate but allows
  the ingest to proceed.

---

## Failure modes

1. **Parser misreads the doc**: agency PDFs have inconsistent
   formats. Parser fallback: if structured extraction fails,
   ingest stores raw_text only and operator manually tags
   fields. Calibration on parser accuracy improves with usage.

2. **Tokenized link sent to wrong recipient**: token is
   workspace-and-rec-scoped. Even if forwarded incorrectly,
   the recipient sees only the in-scope content (no other
   ASIN data, no operator positions, no voice details).

3. **Agency submits incomplete response**: status stays
   awaiting_response for unanswered fields. Operator sees
   "X of Y fields answered" and can re-prompt agency or
   resolve manually.

---

## Version history

Append below this line.

- **v1.0 — 2026-05-18, present commit** — Initial design.
  recommendation_ingest + atlas_evaluation tables specified.
  Field ownership taxonomy (manufacturer / agency /
  amazon_taxonomy / operator_strategic / atlas_calibrated /
  ambiguous). Tokenized response link with 7-day expiration,
  no agency login required. Evaluation prompt formalized
  (5-layer citation chain). Ingest pipeline 7 steps.
