"""Catalog Intel — full-drilldown resolvers.

Each finding in the UI shows a "View N affected ASINs →" button.
Previously that button used the 20-sample cap stored in evidence.
This module re-executes the rule's predicate on-demand against live
workspace state and returns the COMPLETE list of matching ASINs, each
annotated with a `reason_tag` explaining specifically why it matched.

Design principles:
  1. Resolver runs against current workspace state, not the stale
     snapshot the finding was written from. This is intentional —
     if the client uploaded a new snapshot after the finding was
     computed, the drilldown reflects reality, not history.
  2. Every ASIN in the returned list has a `reason_tag` — a short
     phrase (≤40 chars) that shows next to the ASIN in the Listing
     Manager tree. Example: "3 images (threshold: <5)".
  3. Resolvers are OPTIONAL per rule. Aggregate-only rules
     (fill_rate_report, concentration_pareto, a_plus_lift,
     subcategory_rollup) don't have resolvers — they'll return None
     and the UI won't offer a full-drilldown button.

Return shape:
  resolver(cur, workspace_id) -> list of {
    "asin":       str,
    "reason_tag": str,      # short phrase shown next to the ASIN in LM
  }
  or None if the rule doesn't support per-ASIN drilldown.
"""
from __future__ import annotations
from typing import Optional


# ------------------------------------------------------------------
# Individual resolvers
# ------------------------------------------------------------------

def _resolve_dead_inventory(cur, workspace_id: str) -> list:
    """All ASINs in the catalog with zero sessions AND zero units."""
    cur.execute(
        """
        SELECT am.asin
        FROM asin_metadata am
        WHERE am.workspace_id = %s
          AND NOT EXISTS (
            SELECT 1 FROM asin_sales_metrics s
            WHERE s.workspace_id = am.workspace_id
              AND s.asin = am.asin
              AND (s.sessions > 0 OR s.units > 0)
          )
        """,
        (workspace_id,),
    )
    return [{"asin": r[0], "reason_tag": "0 sessions · 0 units"} for r in cur.fetchall() if r[0]]


def _resolve_cohort_split(cur, workspace_id: str) -> list:
    """Cohort of every ASIN (dead / long_tail / core / hero).

    Note: this returns EVERY ASIN, tagged with its cohort. UI filters
    by cohort if needed. This is deliberate — the drilldown is 'show
    me the cohort breakdown across the catalog' not 'show me one cohort'.
    """
    cur.execute(
        """
        WITH sales AS (
          SELECT asin, COALESCE(SUM(sessions), 0) AS sessions,
                       COALESCE(SUM(units), 0)    AS units,
                       COALESCE(SUM(revenue), 0)  AS revenue
          FROM asin_sales_metrics
          WHERE workspace_id = %s
          GROUP BY asin
        ),
        ranked AS (
          SELECT asin, sessions, units, revenue,
                 PERCENT_RANK() OVER (ORDER BY revenue) AS rev_rank
          FROM sales
        )
        SELECT am.asin,
               COALESCE(r.sessions, 0), COALESCE(r.units, 0),
               COALESCE(r.revenue, 0), COALESCE(r.rev_rank, 0)
        FROM asin_metadata am
        LEFT JOIN ranked r ON r.asin = am.asin
        WHERE am.workspace_id = %s
        """,
        (workspace_id, workspace_id),
    )
    out = []
    for asin, sessions, units, revenue, rev_rank in cur.fetchall():
        if not asin:
            continue
        if sessions == 0 and units == 0:
            tag = "dead · 0 sessions · 0 units"
        elif rev_rank >= 0.80:
            tag = f"hero · rev ${revenue:,.0f}"
        elif rev_rank >= 0.10:
            tag = f"core · rev ${revenue:,.0f}"
        else:
            tag = f"long-tail · rev ${revenue:,.0f}"
        out.append({"asin": asin, "reason_tag": tag})
    return out


def _resolve_image_count_dist(cur, workspace_id: str) -> list:
    """ASINs with image_count < 5."""
    cur.execute(
        """
        SELECT asin, (ground_truth_fields->>'image_count')::int AS n
        FROM asin_metadata
        WHERE workspace_id = %s
          AND ground_truth_fields ? 'image_count'
          AND (ground_truth_fields->>'image_count')::int < 5
        """,
        (workspace_id,),
    )
    return [{"asin": r[0], "reason_tag": f"{r[1]} images (threshold: <5)"}
            for r in cur.fetchall() if r[0]]


def _resolve_bullet_completeness_dist(cur, workspace_id: str) -> list:
    """ASINs with fewer than 3 filled bullets."""
    cur.execute(
        """
        SELECT asin,
               (CASE WHEN LENGTH(COALESCE(ground_truth_fields->>'bullet_1','')) > 0 THEN 1 ELSE 0 END +
                CASE WHEN LENGTH(COALESCE(ground_truth_fields->>'bullet_2','')) > 0 THEN 1 ELSE 0 END +
                CASE WHEN LENGTH(COALESCE(ground_truth_fields->>'bullet_3','')) > 0 THEN 1 ELSE 0 END +
                CASE WHEN LENGTH(COALESCE(ground_truth_fields->>'bullet_4','')) > 0 THEN 1 ELSE 0 END +
                CASE WHEN LENGTH(COALESCE(ground_truth_fields->>'bullet_5','')) > 0 THEN 1 ELSE 0 END)
                AS filled
        FROM asin_metadata
        WHERE workspace_id = %s
        """,
        (workspace_id,),
    )
    return [{"asin": r[0], "reason_tag": f"{r[1]} of 5 bullets filled"}
            for r in cur.fetchall() if r[0] and r[1] < 3]


def _resolve_title_length_dist(cur, workspace_id: str) -> list:
    """ASINs with title > 200 OR title < 60."""
    cur.execute(
        """
        SELECT asin, LENGTH(ground_truth_fields->>'title') AS n
        FROM asin_metadata
        WHERE workspace_id = %s
          AND ground_truth_fields ? 'title'
          AND (LENGTH(ground_truth_fields->>'title') > 200
               OR LENGTH(ground_truth_fields->>'title') < 60)
        """,
        (workspace_id,),
    )
    out = []
    for asin, n in cur.fetchall():
        if not asin:
            continue
        if n > 200:
            tag = f"title {n} chars (>200 truncation risk)"
        else:
            tag = f"title {n} chars (<60 thin)"
        out.append({"asin": asin, "reason_tag": tag})
    return out


def _resolve_list_price_dist(cur, workspace_id: str) -> list:
    """ASINs whose list_price is > mean + 2·stdev (upper outliers)."""
    cur.execute(
        """
        WITH stats AS (
          SELECT AVG((ground_truth_fields->>'list_price')::numeric) AS mu,
                 STDDEV((ground_truth_fields->>'list_price')::numeric) AS sigma
          FROM asin_metadata
          WHERE workspace_id = %s
            AND ground_truth_fields ? 'list_price'
            AND (ground_truth_fields->>'list_price') ~ '^[0-9]+(\\.[0-9]+)?$'
        )
        SELECT asin, (ground_truth_fields->>'list_price')::numeric AS price
        FROM asin_metadata, stats
        WHERE workspace_id = %s
          AND ground_truth_fields ? 'list_price'
          AND (ground_truth_fields->>'list_price') ~ '^[0-9]+(\\.[0-9]+)?$'
          AND (ground_truth_fields->>'list_price')::numeric > stats.mu + 2 * stats.sigma
        """,
        (workspace_id, workspace_id),
    )
    return [{"asin": r[0], "reason_tag": f"list_price ${r[1]:,.2f} (>2σ)"}
            for r in cur.fetchall() if r[0]]


def _resolve_style_family_concentration(cur, workspace_id: str) -> list:
    """Children of mega-families (>50 children)."""
    cur.execute(
        """
        WITH mega AS (
          SELECT parent_asin, COUNT(*) AS n
          FROM asin_metadata
          WHERE workspace_id = %s AND parent_asin IS NOT NULL
          GROUP BY parent_asin
          HAVING COUNT(*) > 50
        )
        SELECT am.asin, m.n, m.parent_asin
        FROM asin_metadata am
        JOIN mega m ON m.parent_asin = am.parent_asin
        WHERE am.workspace_id = %s
        """,
        (workspace_id, workspace_id),
    )
    return [{"asin": r[0], "reason_tag": f"mega-family {r[2]} ({r[1]} children)"}
            for r in cur.fetchall() if r[0]]


def _resolve_variation_theme_integrity(cur, workspace_id: str) -> list:
    """Children of parents with missing or inconsistent variation_theme."""
    cur.execute(
        """
        WITH problem_parents AS (
          SELECT parent_asin,
                 COUNT(DISTINCT ground_truth_fields->>'variation_theme') AS themes,
                 BOOL_OR(NOT (ground_truth_fields ? 'variation_theme')
                         OR LENGTH(ground_truth_fields->>'variation_theme') = 0) AS has_missing
          FROM asin_metadata
          WHERE workspace_id = %s AND parent_asin IS NOT NULL
          GROUP BY parent_asin
        )
        SELECT am.asin, p.themes, p.has_missing, p.parent_asin
        FROM asin_metadata am
        JOIN problem_parents p ON p.parent_asin = am.parent_asin
        WHERE am.workspace_id = %s
          AND (p.themes > 1 OR p.has_missing)
        """,
        (workspace_id, workspace_id),
    )
    out = []
    for asin, themes, has_missing, parent in cur.fetchall():
        if not asin:
            continue
        if has_missing:
            tag = f"missing variation_theme (parent {parent})"
        else:
            tag = f"inconsistent themes ({themes} distinct in family)"
        out.append({"asin": asin, "reason_tag": tag})
    return out


def _resolve_description_presence(cur, workspace_id: str) -> list:
    """ASINs missing description OR with description length < 200."""
    cur.execute(
        """
        SELECT asin, LENGTH(COALESCE(ground_truth_fields->>'description', '')) AS n
        FROM asin_metadata
        WHERE workspace_id = %s
          AND (NOT (ground_truth_fields ? 'description')
               OR LENGTH(COALESCE(ground_truth_fields->>'description', '')) < 200)
        """,
        (workspace_id,),
    )
    out = []
    for asin, n in cur.fetchall():
        if not asin:
            continue
        if n == 0:
            tag = "no description"
        else:
            tag = f"description {n} chars (<200 thin)"
        out.append({"asin": asin, "reason_tag": tag})
    return out


def _resolve_buy_box_ownership(cur, workspace_id: str) -> list:
    """ASINs where the operator does NOT hold the buy box.

    Determines the likely owner (top winner if >50%), then returns
    every ASIN with a different winner.
    """
    cur.execute(
        """
        SELECT COALESCE(ground_truth_fields->>'buy_box_winner', '') AS winner,
               COUNT(*) AS n
        FROM asin_metadata
        WHERE workspace_id = %s
        GROUP BY winner
        ORDER BY n DESC
        LIMIT 1
        """,
        (workspace_id,),
    )
    row = cur.fetchone()
    if not row:
        return []
    owner_candidate, top_n = row
    if not owner_candidate:
        return []
    # Only treat as owner if > 50% concentration
    cur.execute(
        "SELECT COUNT(*) FROM asin_metadata WHERE workspace_id = %s",
        (workspace_id,),
    )
    total = int((cur.fetchone() or [0])[0])
    if total == 0 or 100 * top_n / total <= 50:
        return []
    owner = owner_candidate
    cur.execute(
        """
        SELECT asin, COALESCE(ground_truth_fields->>'buy_box_winner', '') AS winner
        FROM asin_metadata
        WHERE workspace_id = %s
          AND COALESCE(ground_truth_fields->>'buy_box_winner', '') <> %s
          AND COALESCE(ground_truth_fields->>'buy_box_winner', '') <> ''
        """,
        (workspace_id, owner),
    )
    return [{"asin": r[0], "reason_tag": f"buy box lost to {r[1][:24]}"}
            for r in cur.fetchall() if r[0]]


def _resolve_fabric_material_coverage(cur, workspace_id: str) -> list:
    """ASINs missing fabric_material."""
    cur.execute(
        """
        SELECT asin FROM asin_metadata
        WHERE workspace_id = %s
          AND (NOT (ground_truth_fields ? 'fabric_material')
               OR LENGTH(COALESCE(ground_truth_fields->>'fabric_material', '')) = 0)
        """,
        (workspace_id,),
    )
    return [{"asin": r[0], "reason_tag": "no fabric_material"}
            for r in cur.fetchall() if r[0]]


# ------------------------------------------------------------------
# Registry — which rules support per-ASIN drilldown
# ------------------------------------------------------------------

RULE_RESOLVERS = {
    "dead_inventory":             _resolve_dead_inventory,
    "cohort_split":               _resolve_cohort_split,
    "image_count_dist":           _resolve_image_count_dist,
    "bullet_completeness_dist":   _resolve_bullet_completeness_dist,
    "title_length_dist":          _resolve_title_length_dist,
    "list_price_dist":            _resolve_list_price_dist,
    "style_family_concentration": _resolve_style_family_concentration,
    "variation_theme_integrity":  _resolve_variation_theme_integrity,
    "description_presence":       _resolve_description_presence,
    "buy_box_ownership":          _resolve_buy_box_ownership,
    "fabric_material_coverage":   _resolve_fabric_material_coverage,
}

# Rules that are aggregate-only (no per-ASIN drilldown)
AGGREGATE_ONLY = {
    "fill_rate_report",
    "concentration_pareto",
    "a_plus_lift",
    "subcategory_rollup",
}


def resolve_full_asins(cur, rule_name: str, workspace_id: str) -> Optional[list]:
    """Return the full list of {asin, reason_tag} for a rule, or None
    if the rule is aggregate-only.
    """
    if rule_name in AGGREGATE_ONLY:
        return None
    fn = RULE_RESOLVERS.get(rule_name)
    if not fn:
        return None
    return fn(cur, workspace_id)


def supported_rule_names() -> list:
    """Rules that have resolvers registered."""
    return sorted(RULE_RESOLVERS.keys())
