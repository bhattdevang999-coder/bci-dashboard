"""Catalog Intel — rule specifications.

This module is the single source of truth for WHAT each of the 15 analyses
actually checks. Every field here is client-facing — if a rule doesn't
have a defensible predicate here, it shouldn't be shipped.

Structure:
  RULE_SPECS[rule_name] = {
    "label":            "Human-readable rule name",
    "category":         "commercial" | "content" | "diagnostic" | "compliance",
    "checks_field":     ["field_a", "field_b"] or [] for cross-catalog rules,
    "data_source":      "catalog" | "sales" | "catalog+sales",
    "predicate":        "Plain-English condition that triggers a match",
    "sql_predicate":    "The exact SQL predicate as executed",
    "threshold":        "Threshold applied (or 'no threshold — structural report')",
    "severity_logic":   "How severity is assigned",
    "min_coverage":     "Minimum data coverage required to trust the result",
    "verify_query":     "Standalone SQL a client can run against their own DB
                         to independently reproduce the count",
  }

Every finding written by run_all() carries this spec inline in
evidence_json.rule_definition, so an operator or agency reading the
raw finding can see exactly why it fired without needing dashboard access.
"""
from __future__ import annotations
from typing import Optional


RULE_SPECS: dict = {
    # ─────────────────────────────────────────────────────────────
    # Diagnostic
    # ─────────────────────────────────────────────────────────────
    "fill_rate_report": {
        "label": "Fill-rate report",
        "category": "diagnostic",
        "checks_field": [],
        "data_source": "catalog",
        "predicate": (
            "For each column tracked in ground_truth_fields, computes "
            "(non-null non-empty count) / (total ASINs) × 100."
        ),
        "sql_predicate": (
            "COUNT(*) FILTER (WHERE ground_truth_fields ? '<field>' "
            "AND LENGTH(ground_truth_fields->>'<field>') > 0) "
            "/ COUNT(*)::float"
        ),
        "threshold": (
            "No threshold — structural report. Fields <5% are flagged as "
            "'effectively missing' in the summary."
        ),
        "severity_logic": "Always 'info' — this rule reports state, not a defect.",
        "min_coverage": "None — runs on any catalog with ≥1 ASIN.",
        "verify_query": (
            "SELECT column_name, "
            "  100.0 * COUNT(*) FILTER (WHERE value IS NOT NULL AND value <> '') "
            "  / COUNT(*) AS fill_pct "
            "FROM asin_metadata, "
            "     LATERAL jsonb_each_text(ground_truth_fields) AS f(column_name, value) "
            "WHERE workspace_id = $1 GROUP BY column_name ORDER BY fill_pct DESC;"
        ),
    },

    # ─────────────────────────────────────────────────────────────
    # Commercial
    # ─────────────────────────────────────────────────────────────
    "concentration_pareto": {
        "label": "Revenue concentration (Pareto)",
        "category": "commercial",
        "checks_field": ["sales.revenue"],
        "data_source": "sales",
        "predicate": (
            "Ranks ASINs by revenue descending, computes cumulative revenue, "
            "reports the count of ASINs required to cross 50%, 80%, 90% of "
            "total revenue."
        ),
        "sql_predicate": (
            "SELECT asin, revenue, "
            "  SUM(revenue) OVER (ORDER BY revenue DESC) / SUM(revenue) OVER () "
            "  AS cum_share "
            "FROM asin_sales_metrics WHERE workspace_id = $1 AND revenue > 0"
        ),
        "threshold": "No threshold — structural report.",
        "severity_logic": "Always 'info' — reports state.",
        "min_coverage": "Requires ≥20% of catalog to have sales data (else the Pareto is unreliable).",
        "verify_query": (
            "WITH r AS (SELECT asin, revenue, SUM(revenue) OVER (ORDER BY revenue DESC) "
            "AS cum, SUM(revenue) OVER () AS total FROM asin_sales_metrics "
            "WHERE workspace_id = $1 AND revenue > 0) "
            "SELECT MIN(rn) FILTER (WHERE cum/total >= 0.5) AS asins_to_50pct "
            "FROM (SELECT *, ROW_NUMBER() OVER (ORDER BY revenue DESC) rn FROM r) s;"
        ),
    },
    "dead_inventory": {
        "label": "Dead inventory",
        "category": "commercial",
        "checks_field": ["sales.sessions", "sales.units"],
        "data_source": "catalog+sales",
        "predicate": (
            "ASIN exists in catalog but has zero sessions AND zero units "
            "for the entire snapshot period (i.e., no traffic and no sales)."
        ),
        "sql_predicate": (
            "am.asin IN (SELECT asin FROM asin_metadata WHERE workspace_id = $1) "
            "AND NOT EXISTS (SELECT 1 FROM asin_sales_metrics s "
            "WHERE s.workspace_id = $1 AND s.asin = am.asin "
            "AND (s.sessions > 0 OR s.units > 0))"
        ),
        "threshold": "sessions = 0 AND units = 0 across the entire period covered by the snapshot.",
        "severity_logic": "high if dead_pct > 50%, medium otherwise.",
        "min_coverage": "Requires ≥20% of catalog with sales rows to be meaningful.",
        "verify_query": (
            "SELECT COUNT(*) AS dead_count FROM asin_metadata am "
            "WHERE am.workspace_id = $1 AND NOT EXISTS ("
            "  SELECT 1 FROM asin_sales_metrics s "
            "  WHERE s.workspace_id = am.workspace_id AND s.asin = am.asin "
            "  AND (s.sessions > 0 OR s.units > 0));"
        ),
    },
    "cohort_split": {
        "label": "Cohort split (dead / long-tail / core / hero)",
        "category": "commercial",
        "checks_field": ["sales.sessions", "sales.units", "sales.revenue"],
        "data_source": "sales",
        "predicate": (
            "Classifies each ASIN into one of four cohorts: "
            "dead (0 sessions AND 0 units), "
            "long_tail (has activity, revenue below the top 90 percentile), "
            "core (revenue in top 90 percentile but not top 20), "
            "hero (revenue in top 20 percentile)."
        ),
        "sql_predicate": (
            "CASE "
            "  WHEN sessions=0 AND units=0 THEN 'dead' "
            "  WHEN revenue < percentile_cont(0.90) THEN 'long_tail' "
            "  WHEN revenue < percentile_cont(0.20) THEN 'core' "
            "  ELSE 'hero' END"
        ),
        "threshold": "Structural — percentiles computed from the snapshot.",
        "severity_logic": "high if dead_pct > 60%, medium if 30-60%, low otherwise.",
        "min_coverage": "≥20% of catalog with sales data.",
        "verify_query": (
            "SELECT cohort, COUNT(*) FROM ("
            "  SELECT CASE WHEN sessions=0 AND units=0 THEN 'dead' "
            "  WHEN revenue < PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY revenue) OVER () "
            "    THEN 'long_tail' "
            "  WHEN revenue < PERCENTILE_CONT(0.20) WITHIN GROUP (ORDER BY revenue) OVER () "
            "    THEN 'core' ELSE 'hero' END AS cohort "
            "  FROM asin_sales_metrics WHERE workspace_id = $1) t GROUP BY cohort;"
        ),
    },

    # ─────────────────────────────────────────────────────────────
    # Content
    # ─────────────────────────────────────────────────────────────
    "a_plus_lift": {
        "label": "A+ content revenue lift",
        "category": "content",
        "checks_field": ["a_plus_status", "sales.revenue"],
        "data_source": "catalog+sales",
        "predicate": (
            "Among parents that have BOTH A+ enabled and A+ disabled children, "
            "computes the mean revenue-per-child for each group. Reports the "
            "lift multiplier (A+ mean / non-A+ mean)."
        ),
        "sql_predicate": (
            "SELECT parent_asin, AVG(revenue) FILTER (WHERE a_plus_status = 'enabled') "
            "AS aplus_rev, AVG(revenue) FILTER (WHERE a_plus_status <> 'enabled') "
            "AS no_aplus_rev FROM ... WHERE both groups have ≥1 child GROUP BY parent_asin"
        ),
        "threshold": "No threshold — reports the lift ratio.",
        "severity_logic": (
            "info if lift ≥ 1.5×, low if 1.0-1.5×, medium if <1.0× (A+ is UNDERPERFORMING)."
        ),
        "min_coverage": "≥80% of ASINs have a_plus_status field populated AND ≥20% of catalog has sales.",
        "verify_query": (
            "-- Same-parent A+ vs non-A+ comparison — see catalog_intel_runner.run_a_plus_lift"
        ),
    },
    "image_count_dist": {
        "label": "Image count distribution",
        "category": "content",
        "checks_field": ["image_count"],
        "data_source": "catalog",
        "predicate": "Histogram of image_count. ASINs with fewer than 5 images are flagged.",
        "sql_predicate": (
            "SELECT (ground_truth_fields->>'image_count')::int AS n, COUNT(*) "
            "FROM asin_metadata WHERE workspace_id = $1 "
            "AND ground_truth_fields ? 'image_count' GROUP BY n"
        ),
        "threshold": (
            "image_count < 5 flagged. Amazon recommends 7-9. 5 is the practical floor "
            "for premium categories."
        ),
        "severity_logic": (
            "medium if under_5_pct > 30% of catalog, low otherwise. "
            "Aggregate finding only — per-ASIN flags are held in the drilldown."
        ),
        "min_coverage": "≥80% of ASINs have image_count populated.",
        "verify_query": (
            "SELECT COUNT(*) FILTER (WHERE (ground_truth_fields->>'image_count')::int < 5) "
            "AS under_5, COUNT(*) AS total FROM asin_metadata "
            "WHERE workspace_id = $1 AND ground_truth_fields ? 'image_count';"
        ),
    },
    "bullet_completeness_dist": {
        "label": "Bullet completeness distribution",
        "category": "content",
        "checks_field": ["bullet_1", "bullet_2", "bullet_3", "bullet_4", "bullet_5"],
        "data_source": "catalog",
        "predicate": (
            "For each ASIN, count how many of the 5 bullet slots are populated "
            "(non-empty). Reports the distribution + flag rate for ASINs with <3 bullets."
        ),
        "sql_predicate": (
            "SELECT SUM(CASE WHEN LENGTH(COALESCE(ground_truth_fields->>'bullet_'||i, '')) > 0 "
            "THEN 1 ELSE 0 END) AS filled_count FROM asin_metadata, generate_series(1,5) i"
        ),
        "threshold": "filled_count < 3 flagged. Amazon caps display at 5; <3 is thin content.",
        "severity_logic": "medium if under_3_pct > 20%, low otherwise.",
        "min_coverage": "≥50% of ASINs have at least one bullet field.",
        "verify_query": (
            "SELECT COUNT(*) FILTER (WHERE bullets_filled < 3) AS thin_bullets, COUNT(*) total "
            "FROM (SELECT (CASE WHEN LENGTH(COALESCE(ground_truth_fields->>'bullet_1','')) > 0 THEN 1 ELSE 0 END + "
            "  CASE WHEN LENGTH(COALESCE(ground_truth_fields->>'bullet_2','')) > 0 THEN 1 ELSE 0 END + "
            "  CASE WHEN LENGTH(COALESCE(ground_truth_fields->>'bullet_3','')) > 0 THEN 1 ELSE 0 END + "
            "  CASE WHEN LENGTH(COALESCE(ground_truth_fields->>'bullet_4','')) > 0 THEN 1 ELSE 0 END + "
            "  CASE WHEN LENGTH(COALESCE(ground_truth_fields->>'bullet_5','')) > 0 THEN 1 ELSE 0 END) "
            "  AS bullets_filled FROM asin_metadata WHERE workspace_id = $1) s;"
        ),
    },
    "title_length_dist": {
        "label": "Title length distribution",
        "category": "content",
        "checks_field": ["title"],
        "data_source": "catalog",
        "predicate": (
            "Character length of title field. Amazon truncates at 200 in most "
            "browse contexts; titles under 60 are typically incomplete."
        ),
        "sql_predicate": "LENGTH(ground_truth_fields->>'title')",
        "threshold": (
            "over_200 flagged (Amazon browse truncation risk). "
            "under_60 flagged (thin content — missing brand, feature, or use)."
        ),
        "severity_logic": (
            "medium if (over_200_pct + under_60_pct) > 30% of catalog, low otherwise."
        ),
        "min_coverage": "≥80% of ASINs have title populated.",
        "verify_query": (
            "SELECT COUNT(*) FILTER (WHERE LENGTH(ground_truth_fields->>'title') > 200) AS over_200, "
            "COUNT(*) FILTER (WHERE LENGTH(ground_truth_fields->>'title') < 60) AS under_60, "
            "COUNT(*) AS total FROM asin_metadata WHERE workspace_id = $1 "
            "AND ground_truth_fields ? 'title';"
        ),
    },
    "list_price_dist": {
        "label": "List price distribution + outliers",
        "category": "commercial",
        "checks_field": ["list_price"],
        "data_source": "catalog",
        "predicate": (
            "Distribution of list_price across the catalog. Outliers = price > "
            "mean + 2·stdev (upper cutoff)."
        ),
        "sql_predicate": (
            "(ground_truth_fields->>'list_price')::numeric > "
            "(mean_price + 2 * stddev_price)"
        ),
        "threshold": "2 standard deviations above the mean.",
        "severity_logic": "info — outliers reported but not flagged as defects (may be intentional premium tier).",
        "min_coverage": "≥50% of ASINs have list_price populated.",
        "verify_query": (
            "WITH stats AS (SELECT AVG((ground_truth_fields->>'list_price')::numeric) mu, "
            "STDDEV((ground_truth_fields->>'list_price')::numeric) sigma "
            "FROM asin_metadata WHERE workspace_id = $1 AND ground_truth_fields ? 'list_price') "
            "SELECT asin, (ground_truth_fields->>'list_price')::numeric AS price "
            "FROM asin_metadata, stats WHERE workspace_id = $1 "
            "AND (ground_truth_fields->>'list_price')::numeric > mu + 2 * sigma;"
        ),
    },
    "subcategory_rollup": {
        "label": "Subcategory rollup",
        "category": "commercial",
        "checks_field": ["subcategory"],
        "data_source": "catalog",
        "predicate": "GROUP BY subcategory — reports ASIN count and (if sales) revenue share per subcategory.",
        "sql_predicate": (
            "GROUP BY ground_truth_fields->>'subcategory' "
            "ORDER BY COUNT(*) DESC"
        ),
        "threshold": "No threshold — structural report.",
        "severity_logic": "info.",
        "min_coverage": "≥50% of ASINs have subcategory populated.",
        "verify_query": (
            "SELECT ground_truth_fields->>'subcategory' AS subcat, COUNT(*) AS asin_count "
            "FROM asin_metadata WHERE workspace_id = $1 "
            "GROUP BY subcat ORDER BY asin_count DESC;"
        ),
    },
    "style_family_concentration": {
        "label": "Style family concentration",
        "category": "commercial",
        "checks_field": ["parent_asin"],
        "data_source": "catalog",
        "predicate": (
            "Groups ASINs by parent_asin, reports family sizes. Families with >50 "
            "children are flagged as 'mega-families' (harder to merchandise, "
            "cannibalization risk)."
        ),
        "sql_predicate": (
            "SELECT parent_asin, COUNT(*) AS child_count "
            "FROM asin_metadata WHERE workspace_id = $1 "
            "AND parent_asin IS NOT NULL GROUP BY parent_asin"
        ),
        "threshold": "child_count > 50 flagged as mega-family.",
        "severity_logic": "medium if any mega-families exist, low otherwise.",
        "min_coverage": "≥50% of ASINs have parent_asin populated.",
        "verify_query": (
            "SELECT parent_asin, COUNT(*) FROM asin_metadata "
            "WHERE workspace_id = $1 AND parent_asin IS NOT NULL "
            "GROUP BY parent_asin HAVING COUNT(*) > 50 ORDER BY COUNT(*) DESC;"
        ),
    },
    "variation_theme_integrity": {
        "label": "Variation theme integrity",
        "category": "content",
        "checks_field": ["variation_theme", "parent_asin"],
        "data_source": "catalog",
        "predicate": (
            "For each parent family: (a) parent has variation_theme set, "
            "(b) all children share the same variation_theme. Flags parents "
            "with missing OR inconsistent themes across children."
        ),
        "sql_predicate": (
            "COUNT(DISTINCT ground_truth_fields->>'variation_theme') > 1 "
            "OR ground_truth_fields->>'variation_theme' IS NULL"
        ),
        "threshold": "No threshold — every mismatch or missing value is a finding.",
        "severity_logic": "high if inconsistent_pct > 10%, medium otherwise.",
        "min_coverage": "≥50% of ASINs have both parent_asin and variation_theme.",
        "verify_query": (
            "SELECT parent_asin, COUNT(DISTINCT ground_truth_fields->>'variation_theme') AS themes "
            "FROM asin_metadata WHERE workspace_id = $1 AND parent_asin IS NOT NULL "
            "GROUP BY parent_asin HAVING COUNT(DISTINCT ground_truth_fields->>'variation_theme') > 1;"
        ),
    },
    "description_presence": {
        "label": "Description presence + length",
        "category": "content",
        "checks_field": ["description"],
        "data_source": "catalog",
        "predicate": (
            "ASIN has a non-empty description field. Also reports average length "
            "and count of descriptions under 200 characters (thin content)."
        ),
        "sql_predicate": (
            "ground_truth_fields ? 'description' "
            "AND LENGTH(ground_truth_fields->>'description') > 0"
        ),
        "threshold": "Missing OR length < 200 = thin content.",
        "severity_logic": "high if pct_with < 70%, medium otherwise.",
        "min_coverage": "None — this rule reports coverage as its output.",
        "verify_query": (
            "SELECT COUNT(*) FILTER (WHERE ground_truth_fields ? 'description' "
            "AND LENGTH(ground_truth_fields->>'description') > 0) AS with_desc, "
            "COUNT(*) FILTER (WHERE LENGTH(ground_truth_fields->>'description') < 200 "
            "AND LENGTH(ground_truth_fields->>'description') > 0) AS short_desc, "
            "COUNT(*) AS total FROM asin_metadata WHERE workspace_id = $1;"
        ),
    },
    "buy_box_ownership": {
        "label": "Buy box ownership",
        "category": "commercial",
        "checks_field": ["buy_box_winner"],
        "data_source": "catalog",
        "predicate": (
            "GROUP BY buy_box_winner. If a single winner holds >50%, treat as "
            "the operator and report ownership pct. Otherwise flag as fragmented."
        ),
        "sql_predicate": (
            "SELECT ground_truth_fields->>'buy_box_winner' AS winner, COUNT(*) "
            "FROM asin_metadata WHERE workspace_id = $1 GROUP BY winner"
        ),
        "threshold": "likely_owner_pct < 80% = potential 3P undercutting risk.",
        "severity_logic": (
            "medium if likely_owner_pct < 80% AND catalog has ≥1 ASIN, low otherwise."
        ),
        "min_coverage": "≥50% of ASINs have buy_box_winner populated.",
        "verify_query": (
            "SELECT ground_truth_fields->>'buy_box_winner' AS winner, COUNT(*) AS n "
            "FROM asin_metadata WHERE workspace_id = $1 GROUP BY winner ORDER BY n DESC;"
        ),
    },
    "fabric_material_coverage": {
        "label": "Fabric / material coverage",
        "category": "compliance",
        "checks_field": ["fabric_material"],
        "data_source": "catalog",
        "predicate": (
            "For apparel catalogs: fabric_material populated. Amazon requires "
            "fabric composition on apparel; missing values are a suppression risk."
        ),
        "sql_predicate": (
            "ground_truth_fields ? 'fabric_material' "
            "AND LENGTH(ground_truth_fields->>'fabric_material') > 0"
        ),
        "threshold": "Missing = defect (compliance rule, not a discretionary threshold).",
        "severity_logic": "Always low in v1 (informational — until we know if the catalog is apparel).",
        "min_coverage": "None — reports coverage as output.",
        "verify_query": (
            "SELECT COUNT(*) FILTER (WHERE ground_truth_fields ? 'fabric_material' "
            "AND LENGTH(ground_truth_fields->>'fabric_material') > 0) AS filled, "
            "COUNT(*) AS total FROM asin_metadata WHERE workspace_id = $1;"
        ),
    },
}


def get_rule_spec(rule_name: str) -> Optional[dict]:
    """Return the full spec dict for a rule, or None if unregistered."""
    return RULE_SPECS.get(rule_name)


def get_rule_definition_for_finding(rule_name: str) -> dict:
    """Compact rule-definition subset attached inline to each finding's evidence.

    Kept small on purpose — the full spec is available via the /rules API.
    This is what gets embedded so a finding is self-contained and inspectable
    even if pulled out of the dashboard (CSV export, agency handoff, etc.).
    """
    spec = RULE_SPECS.get(rule_name)
    if not spec:
        return {"rule_name": rule_name, "spec_registered": False}
    return {
        "rule_name": rule_name,
        "label": spec["label"],
        "category": spec["category"],
        "data_source": spec["data_source"],
        "predicate": spec["predicate"],
        "threshold": spec["threshold"],
        "severity_logic": spec["severity_logic"],
        "checks_field": spec["checks_field"],
    }


def list_registered_rules() -> list:
    """List of {rule_name, label, category} — the catalog index."""
    return [
        {"rule_name": name, "label": s["label"], "category": s["category"]}
        for name, s in RULE_SPECS.items()
    ]
