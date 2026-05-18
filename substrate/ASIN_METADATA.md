# ASIN_METADATA.md — Ground Truth Per ASIN

**Status:** design (pre-build)
**Author:** Devang / Atlas
**Date:** 2026-05-18
**Milestone:** M2 (Days 4–5)

---

## Purpose

Physical product facts and Amazon backend fields per ASIN, stored
as first-class substrate. This is the canonical source of truth
the LLM reads from in Layer 1 of every reasoning chain. Not a
recommendation. Not opinion. Facts about the product as supplied
by the manufacturer or determined by Amazon's category taxonomy.

This is what stops NIS from re-deriving the fabric composition
on every generation, and stops the agency's PDF from being
re-parsed every time someone asks a question about Velune.

---

## Schema

```sql
CREATE TABLE IF NOT EXISTS asin_metadata (
  workspace_id        TEXT NOT NULL,
  asin                TEXT NOT NULL,

  -- Variation structure
  parent_asin         TEXT,
                      -- nullable for parent listings; populated
                      -- on children
  variation_family    TEXT,
                      -- e.g., 'velune_pocket' | 'velune_no_pocket'
  variation_axes      JSONB,
                      -- e.g., {"color": "Midnight Black",
                      --        "size": "M"}

  -- Ground truth fields, structured per Amazon category
  ground_truth_fields JSONB NOT NULL DEFAULT '{}'::jsonb,
                      -- See "Field schema" below

  -- Source attribution per field (for audit + confirmation flow)
  field_sources       JSONB NOT NULL DEFAULT '{}'::jsonb,
                      -- e.g., {
                      --   "material": {
                      --     "value": "79% Nylon, 21% Spandex",
                      --     "source": "factory_spec_2026_05_15",
                      --     "confirmed_by_operator": true,
                      --     "confirmed_at": "2026-05-18T12:00:00Z"
                      --   },
                      --   "fabric_gsm": {
                      --     "value": null,
                      --     "source": null,
                      --     "confirmed_by_operator": false
                      --   }
                      -- }

  -- Versioning + audit
  revision            INTEGER NOT NULL DEFAULT 1,
  set_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  set_by              TEXT,
  last_confirmed_at   TIMESTAMPTZ,
                      -- bumped when operator runs a full
                      -- confirmation pass

  meta                JSONB DEFAULT '{}'::jsonb,
  PRIMARY KEY (workspace_id, asin)
);

CREATE INDEX IF NOT EXISTS idx_asin_metadata_family
  ON asin_metadata (workspace_id, variation_family);

CREATE INDEX IF NOT EXISTS idx_asin_metadata_parent
  ON asin_metadata (workspace_id, parent_asin)
  WHERE parent_asin IS NOT NULL;
```

---

## Field schema (Amazon backend fields, apparel category)

Initial field set covers Amazon's apparel-leggings backend.
This is configurable per category — Atlas reads category from
`workspace_id` settings or from the parent ASIN.

```yaml
# Standard apparel-leggings fields
asin_metadata.ground_truth_fields:
  # Identity
  product_name:                       string
  product_type:                       string
  brand:                              string
  manufacturer:                       string

  # Material & construction
  material:                           string
  fabric_type:                        string
  fabric_gsm:                         number  (often unknown)
  weave_type:                         string
  apparel_fabric_stretch:             string
  fabric_distressing:                 string
  lining_description:                 string

  # Dimensions & fit
  rise_height_inches:                 number
  rise_style:                         string
  waist_style:                        string
  fit_type:                           string
  leg_style:                          string
  front_style:                        string
  item_length_description:            string

  # Use cases & claims
  special_features:                   string[]
  lifestyle:                          string[]
  specific_uses:                      string[]
  sport_type:                         string
  theme:                              string
  fashion_decade:                     string
  upf:                                string

  # Variation axes (also stored in variation_axes JSONB)
  color_name:                         string
  color_map:                          string
  size:                               string
  bottoms_size_to_range:              string

  # Pocket details
  pocket_description:                 string
  pocket_count:                       number
  pocket_type:                        string  -- 'side_seam',
                                              -- 'hidden_waistband', etc.

  # Care & compliance
  care_instructions:                  string
  care_temp:                          string
  care_dry_method:                    string
  country_of_origin:                  string

  # Closure & part info
  apparel_closure_orientation:        string
  pants_form_type:                    string
  number_of_items:                    number
  item_package_quantity:              number
  part_number:                        string  -- internal SKU
  embellishment_feature:              string
  league_name:                        string
  team_name:                          string
  pattern:                            string
```

---

## Parent-child inheritance

Children inherit ground_truth_fields from parent except for
explicitly variation-axis fields:
- `color_name`, `color_map`, `size`, `pocket_description`
  (when family-defining), `pattern` (if differing per colorway)

`build_context()` resolves inheritance at read time. The
`asin_metadata` table stores child-specific overrides only.
Parent's full set is the base; child's stored fields override.

For Velune at launch:
```
parent_asin: B0VEL-PKT (Velune Pocket Family)
  full ground_truth_fields populated
  variation_axes: null
  pocket_description: "Hidden Waistband Pocket"
  pocket_count: 1
  pocket_type: "hidden_waistband"

child_asin: B0VEL-PKT-BLK-M
  parent_asin: B0VEL-PKT
  variation_axes: {"color": "Midnight Black", "size": "M"}
  ground_truth_fields: {
    "color_name": "Midnight Black",
    "color_map": "Black",
    "size": "M"
  }
  -- everything else inherited from parent at read time
```

---

## Cost confirmation flow

Every `field_sources[field].value` has a paired
`field_sources[field].confirmed_by_operator: bool`. The dashboard
shows fields by status:

```
ASIN METADATA — Velune Pocket Family (B0VEL-PKT)

  Confirmed by operator (12)
    material              79% Nylon, 21% Spandex   factory ✓
    fabric_type           79% Nylon, 21% Spandex   factory ✓
    rise_height_inches    11                       factory ✓
    ...

  Awaiting operator confirmation (8)
    care_instructions     "Machine wash"           agency ⚠
    sport_type            "Exercise & Fitness"     agency ⚠
    ...
    [ Review and confirm ]

  Source value disagrees with another source (1)
    pocket_description    factory: hidden_waistband
                          agency: "No Pocket"      ⚠ conflict
    [ Resolve conflict ]

  Not on file (open unknowns) (4)
    fabric_gsm            unknowns#142              factory queue
    upf                   unknowns#143              factory queue
    pocket_dimensions     unknowns#144              factory queue
    ...
```

Atlas's NIS treats unconfirmed fields as `weak_factual` in citation
chains — still cited, but flagged as not yet operator-confirmed.

---

## Onboarding flow (M2 Day 5)

For Velune's 40 ASINs:

```
STEP 1   Create 2 parent records.
         Operator inputs (or Atlas parses from agency PDF):
           - product_name, brand, manufacturer
           - material, weave_type, etc.
           - rise, length, fit
           - pocket details (one parent has, one doesn't)
           - care, country_of_origin
         Each field is marked source='agency_doc' or
         source='factory_spec', confirmed_by_operator=false
         until operator confirms.

STEP 2   Bulk-create 40 children (20 per parent).
         Operator provides the 4 colors and 5 sizes.
         Atlas generates the 4×5 matrix per parent, populates
         variation_axes, sets parent_asin pointer.
         Children have no own ground_truth_fields except
         color/size overrides; everything else inherited.

STEP 3   Operator runs confirmation pass.
         Dashboard shows every parent-level field with its
         source. Operator confirms or edits.
         Confirmation is the moment fields become
         confirmed_by_operator=true.

STEP 4   Unknowns auto-emitted.
         For every required field that's not on file
         (fabric_gsm, upf, etc.), an unknowns row is created
         with evidence_path='factory_spec_sheet' and routed
         to the Factory Questions queue.
```

Estimated operator time: 60–90 minutes for 2 parents + 40
children, including confirmation pass.

---

## Velune-specific pocket conflict resolution

The agency PDF has the known conflict: product_name says "with
Pockets" but pocket_description says "No Pocket." Atlas's parser
will flag this on ingest. The onboarding flow forces resolution:

```
CONFLICT DETECTED in agency-supplied fields:

  product_name           "Novelle Velune ... with Pockets"
  pocket_description     "No Pocket"

  These cannot both apply to the same parent listing.

  Likely cause: agency template was reused across two product
  variants without family-specific edits.

  How to resolve:
  ☐ This template is for the POCKET family. I'll fix
    pocket_description to: ____________
  ☐ This template is for the NO-POCKET family. I'll fix
    product_name to remove "with Pockets".
  ☐ Apply this template to BOTH families with family-specific
    overrides on these two fields.

  [ Continue with resolution ]
```

This is not blocked; operator can defer ("ask agency, defer
launch field"), but the unknown stays open and tagged
launch_blocking until resolved.

---

## What this does NOT do (scope guard)

- Does NOT validate ground_truth_fields against Amazon's actual
  current category taxonomy. Schema fields are static config;
  taxonomy validation is a future feature.
- Does NOT auto-update from Seller Central. Manual entry +
  agency PDF parse only in M2. SP-API listing-detail pull is
  a future write-path.
- Does NOT enforce required-field completeness before allowing
  generation. Generation proceeds with degraded confidence per
  decision_class_requirements; unknowns surface what's missing.

---

## Version history

Append below this line.

- **v1.0 — 2026-05-18, present commit** — Initial design.
  asin_metadata table specified with parent-child inheritance.
  Field schema covers Amazon apparel-leggings backend (~35
  fields). Source attribution + operator confirmation flow.
  Onboarding flow for Velune (2 parents, 40 children).
  Pocket conflict from agency PDF flagged for resolution at
  onboarding time. NIS treats unconfirmed fields as weak_factual.
