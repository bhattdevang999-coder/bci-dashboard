"""Catalog Intel — analysis registry.

Declarative catalog of every analysis Catalog Intel can run.

Each analysis declares its input requirements:
  - required_gtf: JSONB keys that must exist in asin_metadata.ground_truth_fields
  - required_base: base columns on asin_metadata (parent_asin, variation_axes, etc.)
  - required_sales: whether asin_sales_metrics must be populated
  - min_fill_pct: minimum % of ASINs that must have the required data (0-100)
  - runnable_at_fill: fill % above which the analysis runs cleanly
      (5-80% = partial with warning; <5% = blocked)

Coverage matrix computes status per analysis from live data, not from the
uploaded snapshot alone — so re-ingesting an improved file automatically
upgrades the matrix.
"""
from __future__ import annotations


# ============================================================
# v1 analyses — the 15 analyses the module ships with in v1
# ============================================================

ANALYSES = [
    {
        "id": "fill_rate_report",
        "label": "Fill-rate report",
        "description": "Per-column fill rate across the catalog. Always runnable — it IS the coverage layer.",
        "category": "diagnostic",
        "required_gtf": [],
        "required_sales": False,
        "always_runnable": True,
        "output_shape": "table",
    },
    {
        "id": "concentration_pareto",
        "label": "Revenue concentration (Pareto)",
        "description": "Top N% of ASINs = X% of revenue. Reveals long tail vs core.",
        "category": "commercial",
        "required_gtf": [],
        "required_sales": True,
        "required_sales_pct": 20,
        "output_shape": "curve",
    },
    {
        "id": "dead_inventory_cohort",
        "label": "Dead-inventory cohort",
        "description": "ASINs with 0 sessions AND 0 units in the period.",
        "category": "commercial",
        "required_gtf": [],
        "required_sales": True,
        "required_sales_pct": 20,
        "output_shape": "cohort",
    },
    {
        "id": "cohort_split",
        "label": "Cohort split (dead / long-tail / active / core)",
        "description": "Full 4-way cohort classification by session + revenue thresholds.",
        "category": "commercial",
        "required_gtf": [],
        "required_sales": True,
        "required_sales_pct": 20,
        "output_shape": "cohort",
    },
    {
        "id": "a_plus_lift",
        "label": "A+ revenue lift (same-parent comparison)",
        "description": "Among parents with both A+ and non-A+ children, revenue-per-child delta.",
        "category": "content",
        "required_gtf": ["a_plus_status"],
        "required_gtf_pct": 80,
        "required_sales": True,
        "required_sales_pct": 20,
        "output_shape": "comparison",
    },
    {
        "id": "image_count_dist",
        "label": "Image count distribution",
        "description": "Histogram of images per ASIN. Flag <5 and <7.",
        "category": "content",
        "required_gtf": ["image_count"],
        "required_gtf_pct": 80,
        "required_sales": False,
        "output_shape": "histogram",
    },
    {
        "id": "bullet_completeness_dist",
        "label": "Bullet-point completeness",
        "description": "% of ASINs with 1, 2, 3, 4, 5 bullets filled.",
        "category": "content",
        "required_gtf": ["bullet_1"],
        "required_gtf_pct": 50,
        "required_sales": False,
        "output_shape": "histogram",
    },
    {
        "id": "title_length_dist",
        "label": "Title length distribution",
        "description": "Histogram of title char counts. Flag <80 (weak SEO) and >200 (rejected by Amazon).",
        "category": "content",
        "required_gtf": ["title"],
        "required_gtf_pct": 95,
        "required_sales": False,
        "output_shape": "histogram",
    },
    {
        "id": "list_price_dist",
        "label": "List-price distribution",
        "description": "Price bands + outliers.",
        "category": "commercial",
        "required_gtf": ["list_price"],
        "required_gtf_pct": 80,
        "required_sales": False,
        "output_shape": "histogram",
    },
    {
        "id": "subcategory_rollup",
        "label": "Subcategory rollup",
        "description": "Per-subcategory ASIN count, revenue, A+ coverage.",
        "category": "commercial",
        "required_gtf": ["subcategory"],
        "required_gtf_pct": 80,
        "required_sales": False,
        "output_shape": "table",
    },
    {
        "id": "style_family_concentration",
        "label": "Style-family concentration",
        "description": "Children per parent. Orphans, mega-clusters, family health.",
        "category": "structure",
        "required_gtf": [],
        "required_base": ["parent_asin"],
        "required_sales": False,
        "output_shape": "table",
    },
    {
        "id": "variation_theme_integrity",
        "label": "Variation-theme integrity",
        "description": "Missing themes on parents, orphan themes, inconsistent themes within family.",
        "category": "structure",
        "required_gtf": ["variation_theme"],
        "required_gtf_pct": 30,
        "required_sales": False,
        "output_shape": "table",
    },
    {
        "id": "description_presence",
        "label": "Description presence + length",
        "description": "% with description, length distribution.",
        "category": "content",
        "required_gtf": ["description"],
        "required_gtf_pct": 30,
        "required_sales": False,
        "output_shape": "histogram",
    },
    {
        "id": "buy_box_ownership",
        "label": "Buy-box ownership",
        "description": "% of ASINs where the client owns the buy box.",
        "category": "commercial",
        "required_gtf": ["buy_box_winner"],
        "required_gtf_pct": 80,
        "required_sales": False,
        "output_shape": "scalar",
    },
    {
        "id": "fabric_material_coverage",
        "label": "Fabric / material coverage",
        "description": "Apparel-only. % of ASINs with fabric composition set.",
        "category": "compliance",
        "required_gtf": ["fabric_material"],
        "required_gtf_pct": 30,
        "required_sales": False,
        "output_shape": "scalar",
    },
]


# ============================================================
# 360° opportunities — analyses that need data types NOT in the file
# at all (or catastrophically empty). Named so operator can bring more.
# ============================================================

OPPORTUNITIES = [
    {
        "id": "promo_depth",
        "label": "Promo depth analysis",
        "unlocks_when": "Sale Price column is populated",
        "why": "Reveals average discount depth, promo cadence, and which ASINs never go on sale.",
        "gtf_key": "sale_price",
    },
    {
        "id": "compliance_coo",
        "label": "Country-of-Origin compliance",
        "unlocks_when": "Country of Origin column is populated",
        "why": "Required by Amazon for apparel. Missing values risk listing suppression.",
        "gtf_key": "country_of_origin",
    },
    {
        "id": "compliance_care",
        "label": "Care Instructions compliance",
        "unlocks_when": "Care Instructions column is populated",
        "why": "Required by Amazon for apparel. Enables the compliance audit rule.",
        "gtf_key": "care_instructions",
    },
    {
        "id": "search_term_coverage",
        "label": "Backend search-term coverage",
        "unlocks_when": "Backend Keywords column is populated",
        "why": "Reveals which ASINs are indexed for which search terms and where keyword capacity is wasted.",
        "gtf_key": "backend_keywords",
    },
    {
        "id": "review_pareto",
        "label": "Review-count Pareto + CVR correlation",
        "unlocks_when": "Add a review_count column to your export",
        "why": "Single biggest missing signal — reviews correlate with CVR more strongly than any content field.",
        "gtf_key": "review_count",
        "not_in_schema": True,
    },
    {
        "id": "rank_decay",
        "label": "Rank decay detection",
        "unlocks_when": "Add BSR column + upload ≥2 snapshots over time",
        "why": "Detects ASINs that are silently losing rank before revenue drops.",
        "gtf_key": "bsr",
        "not_in_schema": True,
    },
    {
        "id": "ad_efficiency",
        "label": "Ad efficiency (TACoS / ACoS)",
        "unlocks_when": "Add ad_spend and sponsored_units columns",
        "why": "Splits organic from paid revenue and reveals ad-dependent ASINs.",
        "gtf_key": "ad_spend",
        "not_in_schema": True,
    },
    {
        "id": "return_rate_by_asin",
        "label": "Return rate by ASIN",
        "unlocks_when": "Add returns column (units returned)",
        "why": "Identifies size/fit issues, wrong images, or product defects surfacing as returns.",
        "gtf_key": "returns",
        "not_in_schema": True,
    },
    {
        "id": "trend_decay",
        "label": "Trend decay (snapshot-over-snapshot)",
        "unlocks_when": "≥2 sales snapshots on record for this workspace",
        "why": "Compares current period to prior periods to find declining ASINs early.",
        "needs_multiple_snapshots": True,
    },
    {
        "id": "competitor_gap",
        "label": "Competitor keyword gap",
        "unlocks_when": "External keyword benchmark data (Helium 10, Jungle Scout)",
        "why": "Identifies search terms your competitors rank on that you don't.",
        "not_in_schema": True,
    },
]


def compute_coverage(
    *,
    total_asins: int,
    field_fill_counts: dict,          # {gtf_key: count_filled}
    base_field_fill: dict,            # {'parent_asin': count, ...}
    sales_asin_count: int,            # ASINs with any sales row
    snapshot_count: int,              # total snapshots for this workspace
) -> list[dict]:
    """Compute status for every analysis in ANALYSES.

    Returns a list of dicts, each: {
      id, label, status, reason, sample_size, coverage_pct, category,
    }
    """
    if total_asins == 0:
        return [
            {**a, "status": "blocked",
             "reason": "no catalog ingested yet",
             "sample_size": 0, "coverage_pct": 0}
            for a in ANALYSES
        ]

    out = []
    for a in ANALYSES:
        if a.get("always_runnable"):
            out.append({
                **a,
                "status": "runnable",
                "reason": "meta-analysis, no gating",
                "sample_size": total_asins,
                "coverage_pct": 100,
            })
            continue

        min_pct = 100
        blocking_field = None
        for k in a.get("required_gtf", []):
            filled = field_fill_counts.get(k, 0)
            pct = 100 * filled / total_asins
            if pct < min_pct:
                min_pct = pct
                blocking_field = k

        base_pct = 100
        base_blocking = None
        for bk in a.get("required_base", []):
            filled = base_field_fill.get(bk, 0)
            pct = 100 * filled / total_asins
            if pct < base_pct:
                base_pct = pct
                base_blocking = bk

        effective_pct = min(min_pct, base_pct)
        blocking = blocking_field or base_blocking

        if a.get("required_sales"):
            # Sales-driven analyses run on whatever ASINs have sales.
            # Blocked only if the sales sheet is essentially empty (< 50 ASINs).
            # A catalog where 87% of ASINs have no sales IS the signal, not a
            # data gap — dead-inventory cohort exists precisely for that case.
            min_asins = a.get("sales_min_asins", 50)
            sales_pct = 100 * sales_asin_count / total_asins if total_asins else 0
            if sales_asin_count < min_asins:
                out.append({
                    **a,
                    "status": "blocked",
                    "reason": (f"only {sales_asin_count} ASINs have sales "
                                f"data (need ≥{min_asins} for a meaningful "
                                f"analysis)"),
                    "sample_size": sales_asin_count,
                    "coverage_pct": round(sales_pct, 1),
                })
                continue
            # Otherwise: sales analyses run on the ASINs that do have sales.
            # Downgrade to partial if catalog-wide coverage is low but keep runnable.
            sales_status = "runnable" if sales_pct >= 20 else "partial"
            sales_reason = (
                f"runs on {sales_asin_count:,} ASINs with sales "
                f"(the {100-sales_pct:.0f}% without sales IS a finding)"
            )
            # If content-side inputs are also fine, use sales status; otherwise
            # let content-side blocking take precedence below
            if effective_pct >= 5 or not a.get("required_gtf"):
                out.append({
                    **a,
                    "status": sales_status,
                    "reason": sales_reason,
                    "sample_size": sales_asin_count,
                    "coverage_pct": round(sales_pct, 1),
                })
                continue

        need_pct = a.get("required_gtf_pct", 5)
        if effective_pct < 5:
            status, reason = "blocked", (
                f"{blocking or 'required field'}: only "
                f"{effective_pct:.1f}% filled")
        elif effective_pct < need_pct:
            status, reason = "partial", (
                f"{blocking or 'required field'}: {effective_pct:.0f}% filled "
                f"(analysis will run on partial data)")
        else:
            status, reason = "runnable", "all inputs present"

        out.append({
            **a,
            "status": status,
            "reason": reason,
            "sample_size": int(total_asins * effective_pct / 100),
            "coverage_pct": round(effective_pct, 1),
        })
    return out


def compute_opportunities(
    *,
    field_fill_counts: dict,
    total_asins: int,
    snapshot_count: int,
) -> list[dict]:
    """Return opportunities that are still 'not unlocked' with a per-item
    reason. Opportunities already unlocked (i.e., their gtf_key is >80% filled)
    drop off the list."""
    out = []
    for op in OPPORTUNITIES:
        if op.get("needs_multiple_snapshots"):
            if snapshot_count < 2:
                out.append({**op,
                            "unlocked": False,
                            "current_state":
                                f"you have {snapshot_count} snapshot"
                                f"{'' if snapshot_count == 1 else 's'} on record"})
            continue
        if op.get("not_in_schema"):
            out.append({**op, "unlocked": False,
                        "current_state": "column not in current file schema"})
            continue
        key = op.get("gtf_key")
        if not key or total_asins == 0:
            out.append({**op, "unlocked": False,
                        "current_state": "no data yet"})
            continue
        filled = field_fill_counts.get(key, 0)
        pct = 100 * filled / total_asins
        if pct < 5:
            out.append({**op, "unlocked": False,
                        "current_state": f"{pct:.1f}% filled — needs ≥5%"})
        elif pct < 80:
            out.append({**op, "unlocked": False,
                        "current_state": f"{pct:.0f}% filled — needs ≥80% "
                                          f"for full analysis"})
    return out
