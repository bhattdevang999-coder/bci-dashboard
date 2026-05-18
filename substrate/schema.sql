-- Atlas substrate v1.1.1 — Postgres schema
--
-- This is the durable storage layer for the substrate. Replaces the .jsonl
-- files on Render's ephemeral disk. Designed to:
--   1. Preserve the v1.1.1 event model exactly (no schema changes required)
--   2. Enforce workspace_id everywhere so multi-tenancy is structural, not
--      dependent on app-side filters
--   3. Index for the queries the Memory tab will run (by session, by ASIN,
--      by date range, by trigger_type)
--   4. Leave room for Phase 1 additions (image_library, brand_profile,
--      rule_library, outcome_events) without touching v1.1.1 tables
--
-- Migration path: an offline script reads any surviving .jsonl files and
-- inserts each row into the appropriate table. After cutover, the logger
-- writes only to Postgres; .jsonl remains the test-environment backend.

-- ===========================================================================
-- v1.1.1 substrate (existing event model, ported 1:1)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS substrate_events (
    -- Universal columns. event_kind is the v1.1.1 discriminator.
    event_kind          TEXT        NOT NULL CHECK (event_kind IN (
                                        'decision_event',
                                        'operator_response',
                                        'judgment_moment_event',
                                        'session_started',
                                        'session_completed'
                                    )),
    event_id            UUID        NOT NULL,
    workspace_id        TEXT        NOT NULL,
    session_id          TEXT,
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- decision_event columns (NULL for other event_kinds)
    module                  TEXT,
    field_name              TEXT,
    rules_injected          JSONB,
    brand_profile_version   TEXT,
    atlas_output            JSONB,
    overall_confidence      REAL,
    private_scope           BOOLEAN,
    contributable_scope     BOOLEAN,

    -- operator_response columns
    links_to_event_id           UUID,
    operator_action             TEXT,
    operator_value              JSONB,
    operator_scope              TEXT,
    operator_time_to_decision_ms INTEGER,
    operator_comment            TEXT,
    operator_viewed_case        BOOLEAN,

    -- judgment_moment_event columns
    decision_event_id   UUID,
    trigger_type        TEXT,
    surfaced_at         TIMESTAMPTZ,

    -- session lifecycle columns
    operator_id         TEXT,
    started_at          TIMESTAMPTZ,
    ended_at            TIMESTAMPTZ,
    exemplar            BOOLEAN,

    -- Forward-compat metadata; never validated against the locked schema.
    meta                JSONB DEFAULT '{}'::jsonb,

    -- Phase 1: pre_change_snapshot captures the state of the affected
    -- ASIN(s) at the moment a decision was logged. This is the
    -- architecturally-irreversible piece — if we don't capture it at
    -- decision time, outcome attribution becomes impossible. Always
    -- writeable, often empty (we only capture for decisions that
    -- target an ASIN with available outcome data).
    pre_change_snapshot JSONB DEFAULT '{}'::jsonb,

    PRIMARY KEY (event_id, event_kind)
);

-- Index strategy: every query the Memory tab will run goes through one of
-- these. workspace_id is on every index because multi-tenancy filtering
-- happens at every query.
CREATE INDEX IF NOT EXISTS idx_events_workspace_time
    ON substrate_events (workspace_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_events_workspace_session
    ON substrate_events (workspace_id, session_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_events_workspace_kind
    ON substrate_events (workspace_id, event_kind, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_events_links_to
    ON substrate_events (links_to_event_id)
    WHERE links_to_event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_events_decision_event
    ON substrate_events (decision_event_id)
    WHERE decision_event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_events_workspace_module_field
    ON substrate_events (workspace_id, module, field_name)
    WHERE event_kind = 'decision_event';


-- ===========================================================================
-- substrate_sessions — the SessionObject table.
-- One row per session. State transitions (live -> submitted) update in place,
-- but every state change also writes a session_started/session_completed row
-- into substrate_events so the timeline is reconstructable.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS substrate_sessions (
    session_id      TEXT        PRIMARY KEY,
    workspace_id    TEXT        NOT NULL,
    operator_id     TEXT        NOT NULL,
    module          TEXT        NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    state           TEXT        NOT NULL DEFAULT 'live'
                                CHECK (state IN ('live', 'submitted', 'abandoned')),
    operator_notes  TEXT,
    exemplar        BOOLEAN     NOT NULL DEFAULT FALSE,
    meta            JSONB       DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_sessions_workspace_started
    ON substrate_sessions (workspace_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_sessions_workspace_state
    ON substrate_sessions (workspace_id, state, started_at DESC);


-- ===========================================================================
-- Phase 1 additions: tables designed now, written to in Phase 1 features.
-- Leaving them in the same migration so the schema is complete and we don't
-- have to re-coordinate later.
-- ===========================================================================

-- brand_profile: a first-class object. Each version is immutable; updates
-- write new rows. Decision events reference the version string they ran
-- against, so reproducibility holds across profile changes.
CREATE TABLE IF NOT EXISTS brand_profile (
    workspace_id        TEXT        NOT NULL,
    profile_version     TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    brand_name          TEXT,
    category_scope      TEXT,           -- e.g. 'activewear', 'apparel_general'
    tier_scope          TEXT,           -- e.g. 'premium', 'mid', 'value'
    stage_scope         TEXT,           -- e.g. 'launch', 'growth', 'mature'
    voice_rules         JSONB DEFAULT '[]'::jsonb,   -- list of voice/style rules
    banned_words        JSONB DEFAULT '[]'::jsonb,
    required_words      JSONB DEFAULT '[]'::jsonb,
    signature_phrases   JSONB DEFAULT '[]'::jsonb,
    custom              JSONB DEFAULT '{}'::jsonb,   -- free-form additions
    PRIMARY KEY (workspace_id, profile_version)
);

CREATE INDEX IF NOT EXISTS idx_brand_profile_workspace
    ON brand_profile (workspace_id, created_at DESC);


-- rule_library: codified brand rules with applicability scope. Phase 2
-- adds confidence + observation count once the closed loop ships.
CREATE TABLE IF NOT EXISTS rule_library (
    rule_id             TEXT        PRIMARY KEY,
    workspace_id        TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    description         TEXT        NOT NULL,
    -- scope: how broadly does this rule transfer to other brands
    brand_scope         TEXT        CHECK (brand_scope IN
                                       ('brand_specific', 'category_general',
                                        'tier_general', 'stage_general', 'structural')),
    category_scope      TEXT,
    tier_scope          TEXT,
    stage_scope         TEXT,
    status              TEXT        NOT NULL DEFAULT 'experimental'
                                    CHECK (status IN ('experimental', 'active',
                                                      'stale', 'retired')),
    confidence          REAL        DEFAULT 0.5,
    invoked_count       INTEGER     NOT NULL DEFAULT 0,
    overridden_count    INTEGER     NOT NULL DEFAULT 0,
    last_invoked        TIMESTAMPTZ,
    source              JSONB       DEFAULT '{}'::jsonb,  -- which sessions seeded it
    body                JSONB       DEFAULT '{}'::jsonb   -- the rule logic itself
);

CREATE INDEX IF NOT EXISTS idx_rules_workspace_status
    ON rule_library (workspace_id, status);


-- image_library: workspace-scoped image memory. Phase 1 photo-memory work
-- writes here. Perceptual hash for dedup, file hash for exact match.
CREATE TABLE IF NOT EXISTS image_library (
    image_id            UUID        PRIMARY KEY,
    workspace_id        TEXT        NOT NULL,
    uploaded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    uploaded_by         TEXT,           -- operator_id
    file_hash           TEXT        NOT NULL,   -- SHA-256 of file bytes
    perceptual_hash     TEXT,                   -- pHash for near-dup detection
    file_name           TEXT,
    mime_type           TEXT,
    bytes               INTEGER,
    width               INTEGER,
    height              INTEGER,
    dominant_colors     JSONB,
    storage_url         TEXT,           -- where the actual bytes live (S3/Render disk)
    ai_generated        BOOLEAN     NOT NULL DEFAULT FALSE,
    generation_prompt   TEXT,
    generation_model    TEXT,
    generation_params   JSONB,
    meta                JSONB DEFAULT '{}'::jsonb,
    UNIQUE (workspace_id, file_hash)
);

CREATE INDEX IF NOT EXISTS idx_images_workspace_uploaded
    ON image_library (workspace_id, uploaded_at DESC);

CREATE INDEX IF NOT EXISTS idx_images_phash
    ON image_library (workspace_id, perceptual_hash)
    WHERE perceptual_hash IS NOT NULL;


-- image_asin_links: which images appear on which ASINs in which sessions.
CREATE TABLE IF NOT EXISTS image_asin_links (
    image_id            UUID        NOT NULL REFERENCES image_library(image_id) ON DELETE CASCADE,
    workspace_id        TEXT        NOT NULL,
    asin                TEXT        NOT NULL,
    parent_asin         TEXT,
    style_id            TEXT,
    usage_type          TEXT,       -- 'main', 'lifestyle', 'detail', 'size_chart', etc.
    session_id          TEXT,
    first_seen          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (image_id, workspace_id, asin)
);

CREATE INDEX IF NOT EXISTS idx_image_links_asin
    ON image_asin_links (workspace_id, asin);


-- ===========================================================================
-- Phase 1: ingestion_records — the audit trail for every file dropped into
-- the Inputs tab. One row per upload. Carries detected file type, ASINs
-- touched, row counts, who uploaded, when. Used by:
--   - Inputs tab history table
--   - Staleness bar ("PPC bulk: 6 days old")
--   - Pre-change snapshot pipeline (knows what fresh data exists)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS ingestion_records (
    ingestion_id        UUID        PRIMARY KEY,
    workspace_id        TEXT        NOT NULL,
    uploaded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    uploaded_by         TEXT,                       -- operator_id
    -- File-type discriminator. Auto-detected from header signatures.
    -- Free-form to admit new file types as we add ingestors. Common values:
    --   'catalog', 'sales', 'ppc_bulk', 'search_term', 'ad_bulksheet',
    --   'h10_cerebro', 'h10_keyword_tracker', 'brand_analytics_terms',
    --   'returns', 'reviews', 'image'
    file_kind           TEXT        NOT NULL,
    file_name           TEXT,
    file_hash           TEXT,                       -- SHA-256 of file bytes
    bytes               BIGINT,
    -- Date range the file covers (when the ingestor can extract it)
    period_start        TIMESTAMPTZ,
    period_end          TIMESTAMPTZ,
    -- Parser results
    rows_parsed         INTEGER,
    rows_rejected       INTEGER,
    asins_touched       INTEGER,
    -- Detected vs missing fields (for catalog-shaped files)
    detected_fields     JSONB DEFAULT '[]'::jsonb,
    missing_fields      JSONB DEFAULT '[]'::jsonb,
    -- Optional human-readable summary the parser produced
    summary             TEXT,
    -- Free-form metadata (parser version, file format, etc.)
    meta                JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_ingestion_workspace_uploaded
    ON ingestion_records (workspace_id, uploaded_at DESC);

CREATE INDEX IF NOT EXISTS idx_ingestion_workspace_kind
    ON ingestion_records (workspace_id, file_kind, uploaded_at DESC);


-- ===========================================================================
-- Phase 2 additions: outcome_events table. Designed now, populated by
-- closed loop later. Defining it in this migration prevents schema
-- contention when Phase 2 starts.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS outcome_events (
    outcome_id          UUID        PRIMARY KEY,
    workspace_id        TEXT        NOT NULL,
    asin                TEXT        NOT NULL,
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    period_start        TIMESTAMPTZ,
    period_end          TIMESTAMPTZ,
    -- metric this row records
    metric              TEXT        NOT NULL,   -- 'cvr', 'ctr', 'sessions', 'revenue',
                                                -- 'organic_rank', 'acos', 'returns', etc.
    value               DOUBLE PRECISION,
    -- source file/ingestion that produced this observation
    source_file_hash    TEXT,
    source_kind         TEXT,       -- 'business_report', 'ppc_bulk',
                                    -- 'search_term', 'h10_tracker', 'manual'
    -- optional join keys
    keyword             TEXT,
    campaign_id         TEXT,
    ad_group_id         TEXT,
    meta                JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_outcomes_workspace_asin_time
    ON outcome_events (workspace_id, asin, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_outcomes_workspace_metric_time
    ON outcome_events (workspace_id, metric, observed_at DESC);


-- ===========================================================================
-- Operators: lightweight named accounts. Real auth deferred to a later
-- phase. For now, an operator is a (workspace_id, operator_id) tuple plus
-- a display name. Every decision_event carries operator_id so the agency's
-- work is attributable to the specific person on their team.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS operators (
    workspace_id    TEXT        NOT NULL,
    operator_id     TEXT        NOT NULL,
    display_name    TEXT        NOT NULL,
    role            TEXT        NOT NULL DEFAULT 'operator'
                                CHECK (role IN ('owner', 'operator', 'agency', 'viewer')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen       TIMESTAMPTZ,
    active          BOOLEAN     NOT NULL DEFAULT TRUE,
    PRIMARY KEY (workspace_id, operator_id)
);


-- ===========================================================================
-- Schema version marker. Bumped only when the table layout itself changes.
-- v1.1.1 inside the substrate event model maps to v1 of this schema file.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS substrate_schema_version (
    version         TEXT        PRIMARY KEY,
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes           TEXT
);

INSERT INTO substrate_schema_version (version, notes)
    VALUES ('v1', 'Initial Postgres schema. Substrate event model v1.1.1.')
    ON CONFLICT (version) DO NOTHING;


-- ===========================================================================
-- v2 additive migrations.
-- Use ADD COLUMN IF NOT EXISTS so re-applying on a v1 database picks up
-- new columns without recreating tables. Postgres 9.6+ supports this; we
-- target Postgres 17+ on Render so it's safe.
-- ===========================================================================

ALTER TABLE substrate_events
    ADD COLUMN IF NOT EXISTS pre_change_snapshot JSONB DEFAULT '{}'::jsonb;

INSERT INTO substrate_schema_version (version, notes)
    VALUES ('v2', 'Add pre_change_snapshot + ingestion_records (Phase 1).')
    ON CONFLICT (version) DO NOTHING;


-- ===========================================================================
-- v3 — Marketing substrate.
--
-- keyword_library mirrors image_library: workspace-scoped entity table for
-- keywords the operator (or Atlas) has touched. Captures the most-recent
-- known state of each keyword (search volume, current bid, last observed
-- rank/ACOS/spend). Daily observations live in outcome_events; this table
-- is the rolled-up view used by the day-1 wizard and the Memory tab.
--
-- Designed so Phase 2 attribution can join keyword_library → outcome_events
-- without a schema change.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS keyword_library (
    workspace_id        TEXT        NOT NULL,
    keyword_norm        TEXT        NOT NULL,   -- lowercase, whitespace-collapsed
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Display form preserves the operator's original capitalisation.
    keyword             TEXT        NOT NULL,
    match_type          TEXT,           -- 'exact', 'phrase', 'broad', 'auto', NULL
    -- ASINs this keyword has been associated with (denormalised JSONB for
    -- speed; the canonical join lives in outcome_events).
    asins               JSONB DEFAULT '[]'::jsonb,
    -- Latest known metrics. Updated on each ingestion. Historical values
    -- live in outcome_events; this is the 'current state' projection.
    last_search_volume  INTEGER,
    last_organic_rank   INTEGER,
    last_acos           REAL,
    last_spend          REAL,
    last_impressions    INTEGER,
    last_clicks         INTEGER,
    last_orders         INTEGER,
    -- Provenance + free-form metadata.
    first_source_kind   TEXT,   -- 'ppc_bulk', 'search_term', 'manual', 'atlas_suggested'
    last_source_kind    TEXT,
    meta                JSONB DEFAULT '{}'::jsonb,
    PRIMARY KEY (workspace_id, keyword_norm)
);

CREATE INDEX IF NOT EXISTS idx_keyword_library_workspace_seen
    ON keyword_library (workspace_id, last_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_keyword_library_workspace_acos
    ON keyword_library (workspace_id, last_acos)
    WHERE last_acos IS NOT NULL;

INSERT INTO substrate_schema_version (version, notes)
    VALUES ('v3', 'Add keyword_library for Marketing module (Phase 1).')
    ON CONFLICT (version) DO NOTHING;


-- ===========================================================================
-- v4 — Budget substrate.
--
-- Captures planned spend so the closed-loop attribution layer (Phase 2)
-- has a budget context to compare actual spend against. Strictly PPC for
-- v1 — operational costs (photography, A+ design, content rewrites) are
-- explicitly out of scope.
--
-- Granularity v1: one row per (workspace_id, period, scope_type, scope_value).
--   - period      = YYYY-MM (e.g. '2026-05')
--   - scope_type  = 'theme' | 'overall' | 'asin' (asin reserved for v2)
--   - scope_value = the theme name ('branded' | 'feature' | 'competitor'),
--                   the literal '_overall' for total budgets, or an ASIN.
--
-- Schema is designed so per-ASIN budgets can be added in v2 without
-- migration: the same table holds them with scope_type='asin' and
-- scope_value=ASIN. v1 UI just doesn't expose that path yet.
--
-- Every budget set/revise also writes a decision_event with module='budget',
-- field_name='monthly_allocation' so the audit trail in Memory carries
-- budget intent the same way it carries listing intent. The budget table
-- is the rolled-up *current state* projection; substrate_events is the
-- append-only history.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS budget (
    workspace_id   TEXT        NOT NULL,
    period         TEXT        NOT NULL,                    -- 'YYYY-MM'
    scope_type     TEXT        NOT NULL
                                CHECK (scope_type IN ('theme', 'overall', 'asin')),
    scope_value    TEXT        NOT NULL,                    -- theme name | '_overall' | ASIN
    amount         NUMERIC(12, 2) NOT NULL CHECK (amount >= 0),
    currency       TEXT        NOT NULL DEFAULT 'USD',
    set_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    set_by         TEXT,                                    -- operator_id
    notes          TEXT,
    meta           JSONB DEFAULT '{}'::jsonb,
    PRIMARY KEY (workspace_id, period, scope_type, scope_value)
);

CREATE INDEX IF NOT EXISTS idx_budget_workspace_period
    ON budget (workspace_id, period DESC);

CREATE INDEX IF NOT EXISTS idx_budget_workspace_scope
    ON budget (workspace_id, scope_type, scope_value);

INSERT INTO substrate_schema_version (version, notes)
    VALUES ('v4', 'Add budget table for PPC budget tracking (Phase 1).')
    ON CONFLICT (version) DO NOTHING;


-- ===========================================================================
-- Unit Economics (Phase C): operator-supplied cost inputs.
--
-- One row per (workspace_id, asin). Costs are entered manually by the
-- operator; Atlas does not infer them. Empty fields are nullable, not
-- zero — a missing landed_cost means "not on file", not "$0".
--
-- Why a dedicated table (not brand_profile.custom): per-ASIN granularity
-- + need to version cost changes the same way Brand Voice versions, so
-- Memory can answer "when did landed cost change?".
--
-- Every save also writes a decision_event with module='unit_economics',
-- field_name='cost_input' so the audit trail in Memory carries the
-- intent the same way it carries listing intent and voice intent.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS cost_inputs (
    workspace_id     TEXT        NOT NULL,
    asin             TEXT        NOT NULL,
    landed_cost      NUMERIC(12, 4),                          -- per-unit COGS landed
    fba_fee          NUMERIC(12, 4),                          -- per-unit FBA fulfilment fee
    third_pl_fee     NUMERIC(12, 4),                          -- per-unit 3PL prep/storage
    referral_pct     NUMERIC(6, 4),                           -- Amazon referral % (default 0.15)
    map_price        NUMERIC(12, 2),                          -- minimum advertised price
    notes            TEXT,
    revision         INTEGER     NOT NULL DEFAULT 1,
    set_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    set_by           TEXT,                                    -- operator_id
    meta             JSONB DEFAULT '{}'::jsonb,
    PRIMARY KEY (workspace_id, asin)
);

CREATE INDEX IF NOT EXISTS idx_cost_inputs_workspace
    ON cost_inputs (workspace_id, set_at DESC);


-- Brand-level overhead. One row per workspace. Used for the rollup
-- "fixed_overhead_monthly" line above the contribution-margin table.
-- Per Model 1 (UNIT_ECONOMICS.md): fixed costs are NEVER pushed into
-- per-unit contribution margin — they sit above the line at the brand
-- level.

CREATE TABLE IF NOT EXISTS brand_overhead (
    workspace_id          TEXT           NOT NULL PRIMARY KEY,
    fixed_overhead_monthly NUMERIC(12, 2),
    notes                 TEXT,
    revision              INTEGER        NOT NULL DEFAULT 1,
    set_at                TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    set_by                TEXT,
    meta                  JSONB DEFAULT '{}'::jsonb
);

INSERT INTO substrate_schema_version (version, notes)
    VALUES ('v5', 'Unit Economics Phase C: cost_inputs + brand_overhead.')
    ON CONFLICT (version) DO NOTHING;


-- ===========================================================================
-- Schema v6 — Phase 1.5 substrate
--
-- Adds:
--   1. Decision provenance columns on substrate_events (CONTEXT.md)
--   2. Citation tables (CITATION_CHAIN.md)
--   3. unknowns table (UNKNOWNS.md)
--
-- Per design docs committed 2026-05-18.
-- ===========================================================================

-- 1. Decision provenance + citation columns on substrate_events
ALTER TABLE substrate_events
  ADD COLUMN IF NOT EXISTS context_rows_read     TEXT[],
  ADD COLUMN IF NOT EXISTS context_rows_used     TEXT[],
  ADD COLUMN IF NOT EXISTS evidence_strength     TEXT,
  ADD COLUMN IF NOT EXISTS calibration_class     TEXT,
  ADD COLUMN IF NOT EXISTS citations             JSONB,
  ADD COLUMN IF NOT EXISTS citation_outcomes     JSONB,
  ADD COLUMN IF NOT EXISTS confidence_breakdown  JSONB,
  ADD COLUMN IF NOT EXISTS convention_flags      JSONB;

CREATE INDEX IF NOT EXISTS idx_substrate_events_calibration_class
  ON substrate_events (workspace_id, calibration_class, timestamp DESC)
  WHERE calibration_class IS NOT NULL;


-- 2. Citation rejections — operator-driven, future calibration input
CREATE TABLE IF NOT EXISTS citation_rejections (
    rejection_id        TEXT PRIMARY KEY,
    workspace_id        TEXT NOT NULL,
    decision_event_id   TEXT NOT NULL,
    citation_layer      TEXT NOT NULL,    -- factual|strategic|voice|evidence|calibrated_external|convention
    citation_source_id  TEXT NOT NULL,    -- the substrate row id rejected
    reason              TEXT,
    rejected_by         TEXT,
    rejected_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    meta                JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_citation_rejections_layer
  ON citation_rejections (workspace_id, citation_layer, rejected_at DESC);

CREATE INDEX IF NOT EXISTS idx_citation_rejections_source
  ON citation_rejections (workspace_id, citation_source_id);


-- 3. Unknowns — ignorance catalog (UNKNOWNS.md)
CREATE TABLE IF NOT EXISTS unknowns (
    unknown_id           TEXT PRIMARY KEY,
    workspace_id         TEXT NOT NULL,

    scope                TEXT NOT NULL,
                         -- 'global' | 'brand' | 'asin' | 'family'
                         -- | 'decision_class'
    scope_ref            TEXT,
                         -- ASIN | family_id | decision_class name
                         -- nullable when scope='global'

    question             TEXT NOT NULL,

    required_for         TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
                         -- decision_class names this unknown affects

    evidence_path        TEXT NOT NULL,
                         -- 'factory_spec_sheet' | 'agency_response' |
                         -- 'helium10_weekly' | 'a_b_test' |
                         -- 'outcome_measurement' | 'operator_decision' |
                         -- 'declared_unknowable'

    status               TEXT NOT NULL DEFAULT 'open',
                         -- 'open' | 'partial' | 'answered'
                         -- | 'declared_unknowable' | 'expired'

    priority             TEXT NOT NULL DEFAULT 'normal',
                         -- 'launch_blocking' | 'high' | 'normal' | 'low'

    partial_evidence     JSONB DEFAULT '[]'::jsonb,
    answer_value         JSONB,
    answer_source        TEXT,
    answered_at          TIMESTAMPTZ,
    answered_by          TEXT,

    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by_event_id  TEXT,
    created_by_module    TEXT,

    meta                 JSONB DEFAULT '{}'::jsonb
);

-- Active-unknowns lookups
CREATE INDEX IF NOT EXISTS idx_unknowns_status_priority
  ON unknowns (workspace_id, status, priority, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_unknowns_evidence_path
  ON unknowns (workspace_id, status, evidence_path)
  WHERE status IN ('open', 'partial');

CREATE INDEX IF NOT EXISTS idx_unknowns_scope_ref
  ON unknowns (workspace_id, scope, scope_ref)
  WHERE status IN ('open', 'partial');


INSERT INTO substrate_schema_version (version, notes)
    VALUES ('v6', 'Phase 1.5: provenance columns, citation tables, unknowns.')
    ON CONFLICT (version) DO NOTHING;


-- ===========================================================================
-- v7 MIGRATION (M2, 2026-05-18): mode-aware substrate primitives.
--
-- Implements design docs ASIN_METADATA.md, BRAND_POSITION.md,
-- OPERATOR_POSITIONS.md, PRICING_LOGIC.md, RECOMMENDATION_INGEST.md.
-- Six tables:
--   1. asin_metadata             — ground-truth physical/Amazon backend fields
--   2. brand_position            — strategic brand position (one per workspace)
--   3. operator_positions        — operator beliefs/rules as substrate
--   4. pricing_logic             — operator-set floor/ceiling rules
--   5. pricing_decisions         — journal of every price set + outcomes
--   6. competitor_state          — manual competitor observations
-- ===========================================================================

-- 1. asin_metadata
CREATE TABLE IF NOT EXISTS asin_metadata (
    workspace_id          TEXT NOT NULL,
    asin                  TEXT NOT NULL,

    -- Variation structure
    parent_asin           TEXT,
    variation_family      TEXT,
    variation_axes        JSONB,

    -- Ground truth (apparel-leggings field set; see ASIN_METADATA.md)
    ground_truth_fields   JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Source attribution + operator-confirmation flow
    field_sources         JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Versioning + audit
    revision              INTEGER NOT NULL DEFAULT 1,
    set_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    set_by                TEXT,
    last_confirmed_at     TIMESTAMPTZ,

    meta                  JSONB DEFAULT '{}'::jsonb,
    PRIMARY KEY (workspace_id, asin)
);

CREATE INDEX IF NOT EXISTS idx_asin_metadata_family
    ON asin_metadata (workspace_id, variation_family);

CREATE INDEX IF NOT EXISTS idx_asin_metadata_parent
    ON asin_metadata (workspace_id, parent_asin)
    WHERE parent_asin IS NOT NULL;


-- 2. brand_position (one row per workspace)
CREATE TABLE IF NOT EXISTS brand_position (
    workspace_id            TEXT PRIMARY KEY,

    position_statement      TEXT NOT NULL,
    competitor_set          TEXT[] NOT NULL,
    competitor_role         JSONB NOT NULL DEFAULT '{}'::jsonb,
    price_band              JSONB NOT NULL DEFAULT '{}'::jsonb,
    positioning_hypothesis  TEXT,

    pricing_logic_revision  INTEGER,

    review_freq             TEXT NOT NULL DEFAULT 'quarterly',
    last_reviewed_at        TIMESTAMPTZ,
    next_review_at          TIMESTAMPTZ NOT NULL,

    revision                INTEGER NOT NULL DEFAULT 1,
    set_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    set_by                  TEXT NOT NULL,

    meta                    JSONB DEFAULT '{}'::jsonb
);


-- 3. operator_positions (per OPERATOR_POSITIONS.md)
CREATE TABLE IF NOT EXISTS operator_positions (
    position_id           TEXT PRIMARY KEY,
    workspace_id          TEXT NOT NULL,
    operator_id           TEXT NOT NULL DEFAULT 'devang',

    scope                 TEXT NOT NULL,
                          -- 'global'|'brand'|'asin'|'family'
                          -- |'decision_class'|'family_decision_class'
    scope_ref             TEXT,

    claim                 TEXT NOT NULL,
    reasoning             TEXT,

    position_type         TEXT NOT NULL DEFAULT 'strategic',
                          -- 'strategic'|'style'|'hard_refusal'
                          -- |'workflow'|'preference'

    status                TEXT NOT NULL DEFAULT 'active',
                          -- 'active'|'archived'|'superseded'

    superseded_by         TEXT,
    evidence_refs         TEXT[] DEFAULT ARRAY[]::TEXT[],

    revision              INTEGER NOT NULL DEFAULT 1,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by_event_id   TEXT,
    last_reviewed_at      TIMESTAMPTZ,

    meta                  JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_op_positions_scope
    ON operator_positions (workspace_id, status, scope, scope_ref);

CREATE INDEX IF NOT EXISTS idx_op_positions_type
    ON operator_positions (workspace_id, status, position_type);


-- 4. pricing_logic (operator-set rules; scope-keyed)
CREATE TABLE IF NOT EXISTS pricing_logic (
    workspace_id            TEXT NOT NULL,
    scope                   TEXT NOT NULL,            -- 'global'|'family'|'asin'
    scope_ref               TEXT NOT NULL DEFAULT '', -- empty string for global

    floor_rule              JSONB NOT NULL,
    ceiling_rule            JSONB NOT NULL,
    reasoning               TEXT,

    revision                INTEGER NOT NULL DEFAULT 1,
    set_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    set_by                  TEXT NOT NULL,

    ceiling_next_review_at  TIMESTAMPTZ,

    meta                    JSONB DEFAULT '{}'::jsonb,
    PRIMARY KEY (workspace_id, scope, scope_ref)
);


-- 5. pricing_decisions (journal)
CREATE TABLE IF NOT EXISTS pricing_decisions (
    decision_id          TEXT PRIMARY KEY,
    workspace_id         TEXT NOT NULL,
    asin                 TEXT NOT NULL,

    price_set            NUMERIC(12, 2) NOT NULL,
    price_set_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    price_set_by         TEXT NOT NULL,

    floor_at_time        NUMERIC(12, 2),
    ceiling_at_time      NUMERIC(12, 2),
    play_zone_position   TEXT,
                         -- 'below_floor'|'near_floor'|'middle'
                         -- |'near_ceiling'|'above_ceiling'

    goal_regime          TEXT,
                         -- 'launch_velocity'|'margin'|'volume'

    reasoning            TEXT,

    mode                 TEXT NOT NULL,
                         -- 'manual'|'mode1_llm'|'mode2_calibrated'

    outcome_at_30d       JSONB,
    outcome_at_60d       JSONB,
    outcome_at_90d       JSONB,
    pattern_tags         TEXT[] DEFAULT ARRAY[]::TEXT[],

    meta                 JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_pricing_decisions_asin
    ON pricing_decisions (workspace_id, asin, price_set_at DESC);

CREATE INDEX IF NOT EXISTS idx_pricing_decisions_regime
    ON pricing_decisions (workspace_id, goal_regime, price_set_at DESC);


-- 6. competitor_state (manual competitor observations)
CREATE TABLE IF NOT EXISTS competitor_state (
    observation_id        TEXT PRIMARY KEY,
    workspace_id          TEXT NOT NULL,
    competitor_id         TEXT NOT NULL,   -- e.g., 'crz_yoga'

    metric                TEXT NOT NULL,   -- 'price'|'review_count'|'bsr'|'listing_changed'
    value                 JSONB NOT NULL,  -- numeric or structured per metric
    observed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    observed_by           TEXT NOT NULL,
    source                TEXT NOT NULL DEFAULT 'operator_manual',
                          -- 'operator_manual'|'helium10'|'keepa'|'jungle_scout'
    asin                  TEXT,            -- competitor ASIN if known
    notes                 TEXT,

    meta                  JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_competitor_state_lookup
    ON competitor_state (workspace_id, competitor_id, metric, observed_at DESC);


INSERT INTO substrate_schema_version (version, notes)
    VALUES ('v7', 'M2: asin_metadata, brand_position, operator_positions, pricing_logic, pricing_decisions, competitor_state.')
    ON CONFLICT (version) DO NOTHING;


-- ===========================================================================
-- v8 MIGRATION (M4, 2026-05-18): recommendation ingest + agency response.
--
-- Implements RECOMMENDATION_INGEST.md. Every external recommendation
-- lands as substrate; Atlas evaluates each field against the 5-layer
-- citation chain; tokenized response link lets agencies answer inside
-- the system without a dashboard login.
--
-- Tables:
--   recommendation_ingest   — incoming recs + tokenized response link state
--   atlas_evaluation        — per-field verdict, agency response, operator
--                             decision, final value
-- ===========================================================================

CREATE TABLE IF NOT EXISTS recommendation_ingest (
    rec_id                TEXT PRIMARY KEY,
    workspace_id          TEXT NOT NULL,

    source                TEXT NOT NULL,
                          -- 'acme_agency' | 'helium10' | 'operator_note' | ...
    source_tier           TEXT,
                          -- 'top_agency' | 'mid_agency' | 'budget_agency'
                          -- | 'vendor_tool' | 'operator' | 'internal_sop'
    source_contact        TEXT,

    raw_text              TEXT,
    raw_file_path         TEXT,
    raw_file_hash         TEXT,

    parsed_fields         JSONB,
    scope_asins           TEXT[] DEFAULT ARRAY[]::TEXT[],
    scope_confidence      NUMERIC(4, 3),
    rec_type              TEXT,
                          -- 'backend_fields' | 'keyword_list'
                          -- | 'pricing_proposal' | 'listing_copy' | ...

    ingested_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingested_by           TEXT,

    status                TEXT NOT NULL DEFAULT 'pending_evaluation',
                          -- 'pending_evaluation' | 'evaluated'
                          -- | 'awaiting_response' | 'response_received'
                          -- | 'resolved' | 'archived'

    -- Tokenized response link (single-use; expires)
    response_token        TEXT UNIQUE,
    response_token_url    TEXT,
    response_expires_at   TIMESTAMPTZ,
    response_received_at  TIMESTAMPTZ,

    meta                  JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_rec_ingest_status
    ON recommendation_ingest (workspace_id, status, ingested_at DESC);

CREATE INDEX IF NOT EXISTS idx_rec_ingest_source
    ON recommendation_ingest (workspace_id, source, ingested_at DESC);

CREATE INDEX IF NOT EXISTS idx_rec_ingest_token
    ON recommendation_ingest (response_token)
    WHERE response_token IS NOT NULL;


CREATE TABLE IF NOT EXISTS atlas_evaluation (
    eval_id              TEXT PRIMARY KEY,
    rec_id               TEXT NOT NULL
                         REFERENCES recommendation_ingest(rec_id)
                         ON DELETE CASCADE,
    workspace_id         TEXT NOT NULL,

    field_name           TEXT NOT NULL,
    submitted_value      TEXT,
    field_owner          TEXT NOT NULL,
                         -- 'manufacturer' | 'agency' | 'amazon_taxonomy'
                         -- | 'operator_strategic' | 'atlas_calibrated'
                         -- | 'ambiguous'

    verdict              TEXT NOT NULL,
                         -- 'agree' | 'partial' | 'disagree' | 'unknown'
    reasoning            TEXT NOT NULL,
    citations            JSONB DEFAULT '[]'::jsonb,

    proposed_alternative TEXT,
    test_design          TEXT,
    evidence_path        TEXT,

    confidence           NUMERIC(4, 3),
    criticality          TEXT,
                         -- 'launch_blocking' | 'high' | 'normal' | 'low'

    -- Agency response (via tokenized link)
    agency_response      TEXT,
    agency_response_at   TIMESTAMPTZ,
    agency_confidence    INTEGER,    -- 1-5 self-rating

    -- Operator decision
    operator_decision    TEXT,
                         -- 'accept' | 'override' | 'defer' | 'reject'
    operator_decided_at  TIMESTAMPTZ,
    operator_reasoning   TEXT,
    final_value          TEXT,

    evaluated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    meta                 JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_atlas_eval_rec
    ON atlas_evaluation (rec_id, evaluated_at);

CREATE INDEX IF NOT EXISTS idx_atlas_eval_pending
    ON atlas_evaluation (rec_id)
    WHERE operator_decision IS NULL;

CREATE INDEX IF NOT EXISTS idx_atlas_eval_owner
    ON atlas_evaluation (workspace_id, field_owner, evaluated_at DESC);


INSERT INTO substrate_schema_version (version, notes)
    VALUES ('v8', 'M4: recommendation_ingest + atlas_evaluation.')
    ON CONFLICT (version) DO NOTHING;
