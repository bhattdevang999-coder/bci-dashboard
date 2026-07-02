"""Catalog Intel — analysis runner.

Executes the runnable analyses for a workspace and writes findings.

Design principles:
- Aggregate findings are asin=NULL (one row per analysis at catalog level).
  Per-ASIN findings only for critical actionable items (e.g. title <80 chars,
  images <5).
- Every analysis returns a summary dict + list of finding dicts.
- SQL-heavy where possible; row-level iteration only for the analyses that
  need it (Pareto sort, histograms).
- Idempotent: calling run_all() twice deletes prior findings for that
  snapshot_id (or workspace if no snapshot given) and re-writes.

All numeric outputs are Python-native (int/float), never Decimal, so jsonify
works cleanly.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.catalog_intel_runner")


# ============================================================
# Analysis functions
#
# Each returns:
#   {"summary": {...}, "findings": [{...}, ...]}
# where finding is:
#   {"rule_name", "severity", "priority_score", "asin"|None,
#    "evidence_json"|dict, "proposed_fix"|str}
# ============================================================


def _finding(rule_name, severity, priority_score, *,
             asin=None, evidence=None, proposed_fix=None):
    return {
        "rule_name": rule_name,
        "severity": severity,
        "priority_score": float(priority_score),
        "asin": asin,
        "evidence_json": evidence or {},
        "proposed_fix": proposed_fix,
    }


def run_concentration_pareto(cur, workspace_id: str) -> dict:
    """Top N% ASINs = X% of revenue."""
    cur.execute(
        """
        SELECT asin, SUM(COALESCE(revenue, 0)) AS rev
        FROM asin_sales_metrics
        WHERE workspace_id = %s
        GROUP BY asin
        HAVING SUM(COALESCE(revenue, 0)) > 0
        ORDER BY rev DESC
        """,
        (workspace_id,),
    )
    rows = [(r[0], float(r[1])) for r in cur.fetchall()]
    if not rows:
        return {"summary": {"active_asins": 0, "total_revenue": 0},
                "findings": []}

    total_rev = sum(r[1] for r in rows)
    cum = 0.0
    thresholds = {50: None, 80: None, 90: None}
    for i, (_, rev) in enumerate(rows, start=1):
        cum += rev
        pct = 100 * cum / total_rev
        for t in thresholds:
            if thresholds[t] is None and pct >= t:
                thresholds[t] = i

    summary = {
        "active_asins": len(rows),
        "total_revenue": round(total_rev, 2),
        "top_50pct_asins": thresholds[50],
        "top_80pct_asins": thresholds[80],
        "top_90pct_asins": thresholds[90],
    }
    return {
        "summary": summary,
        "findings": [
            _finding(
                "concentration_pareto",
                "strategic",
                1.0,
                evidence=summary,
                proposed_fix=(
                    f"Top {thresholds[50]} ASINs generate 50% of revenue; "
                    f"top {thresholds[80]} generate 80%. Focus content + ad "
                    f"investment on the core, evaluate long tail for pruning."
                ),
            ),
        ],
    }


def run_cohort_split(cur, workspace_id: str) -> dict:
    """dead / long-tail / active / core cohort classification."""
    # Left join catalog → sales; if no sales row, cohort=dead.
    cur.execute(
        """
        SELECT COUNT(*) FROM asin_metadata WHERE workspace_id = %s
        """,
        (workspace_id,),
    )
    total_catalog = int(cur.fetchone()[0] or 0)

    cur.execute(
        """
        SELECT am.asin,
               COALESCE(SUM(sm.sessions), 0) AS sess,
               COALESCE(SUM(sm.units), 0) AS units,
               COALESCE(SUM(sm.revenue), 0) AS rev
        FROM asin_metadata am
        LEFT JOIN asin_sales_metrics sm
          ON sm.workspace_id = am.workspace_id AND sm.asin = am.asin
        WHERE am.workspace_id = %s
        GROUP BY am.asin
        """,
        (workspace_id,),
    )
    rows = cur.fetchall()

    cohorts = {"dead": 0, "long_tail": 0, "active": 0, "core": 0}
    dead_asins = []
    revs = []
    for r in rows:
        sess = int(r[1] or 0)
        units = int(r[2] or 0)
        rev = float(r[3] or 0)
        revs.append((r[0], rev))
        if sess == 0 and units == 0:
            cohorts["dead"] += 1
            if len(dead_asins) < 10:
                dead_asins.append(r[0])
        elif sess < 500 and units < 10:
            cohorts["long_tail"] += 1
        else:
            # Split active vs core by top-decile revenue
            cohorts["active"] += 1

    # Recompute core: top 10% of revenue-earning ASINs
    non_dead_revs = sorted(
        [rev for _, rev in revs if rev > 0], reverse=True
    )
    if non_dead_revs:
        core_cutoff_idx = max(1, len(non_dead_revs) // 10)
        core_cutoff_rev = non_dead_revs[core_cutoff_idx - 1]
        core_count = sum(1 for _, rev in revs if rev >= core_cutoff_rev and rev > 0)
        cohorts["core"] = core_count
        cohorts["active"] = max(0, cohorts["active"] - core_count)

    summary = {
        "total_catalog": total_catalog,
        **cohorts,
        "dead_pct": round(100 * cohorts["dead"] / total_catalog, 1)
                     if total_catalog else 0,
    }

    findings = [
        _finding(
            "cohort_split",
            "strategic",
            1.0,
            evidence=summary,
            proposed_fix=(
                f"{cohorts['dead']} ASINs ({summary['dead_pct']}%) are dead. "
                f"{cohorts['long_tail']} are long-tail low-converters. "
                f"{cohorts['core']} are the revenue core. Focus operator time "
                f"on core; evaluate dead for delisting or A+ investment; "
                f"decide long-tail on a per-cluster basis."
            ),
        ),
    ]

    # Sample of dead ASINs as evidence for the delisting decision
    if dead_asins:
        findings[0]["evidence_json"]["dead_asin_sample"] = dead_asins

    return {"summary": summary, "findings": findings}


def run_a_plus_lift(cur, workspace_id: str) -> dict:
    """Same-parent A+ vs non-A+ children revenue delta."""
    # Fetch parent → children with a_plus_status + revenue
    cur.execute(
        """
        SELECT am.parent_asin,
               am.asin,
               COALESCE(am.ground_truth_fields->>'a_plus_status', '') AS aplus,
               COALESCE(SUM(sm.revenue), 0) AS rev
        FROM asin_metadata am
        LEFT JOIN asin_sales_metrics sm
          ON sm.workspace_id = am.workspace_id AND sm.asin = am.asin
        WHERE am.workspace_id = %s
          AND am.parent_asin IS NOT NULL
        GROUP BY am.parent_asin, am.asin, aplus
        """,
        (workspace_id,),
    )
    rows = cur.fetchall()
    if not rows:
        return {"summary": {"parents_with_mixed": 0}, "findings": []}

    # Group by parent, split children by aplus flag
    families = {}
    def _is_aplus(v):
        s = str(v or "").strip().lower()
        return s in ("yes", "true", "complete", "published", "1", "y")

    for r in rows:
        parent, child, aplus, rev = r[0], r[1], r[2], float(r[3] or 0)
        fam = families.setdefault(parent, {"aplus": [], "no_aplus": []})
        if _is_aplus(aplus):
            fam["aplus"].append(rev)
        else:
            fam["no_aplus"].append(rev)

    # Filter to families with both A+ and non-A+ children
    mixed = {p: v for p, v in families.items()
             if v["aplus"] and v["no_aplus"]}

    if not mixed:
        return {
            "summary": {"parents_with_mixed": 0,
                        "reason": "no parents have both A+ and non-A+ children"},
            "findings": [],
        }

    # Compute revenue per child in each group, averaged across families
    aplus_avgs = []
    non_avgs = []
    for _p, v in mixed.items():
        aplus_avgs.append(sum(v["aplus"]) / len(v["aplus"]))
        non_avgs.append(sum(v["no_aplus"]) / len(v["no_aplus"]))
    avg_aplus = sum(aplus_avgs) / len(aplus_avgs)
    avg_non = sum(non_avgs) / len(non_avgs)
    lift = (avg_aplus / avg_non) if avg_non > 0 else None

    summary = {
        "parents_with_mixed": len(mixed),
        "avg_rev_per_aplus_child": round(avg_aplus, 2),
        "avg_rev_per_non_aplus_child": round(avg_non, 2),
        "lift_ratio": round(lift, 2) if lift else None,
    }
    severity = "high" if lift and lift >= 1.5 else "medium"
    findings = [
        _finding(
            "a_plus_lift",
            severity,
            0.9 if lift and lift >= 1.5 else 0.6,
            evidence=summary,
            proposed_fix=(
                f"Among {len(mixed)} parents with both, A+ children earn "
                f"{lift:.1f}\u00d7 revenue vs non-A+ siblings. Fastest lever: "
                f"add A+ content to the non-A+ children in mixed families."
                if lift else
                f"A+ vs non-A+ comparison inconclusive ({len(mixed)} mixed families)."
            ),
        ),
    ]
    return {"summary": summary, "findings": findings}


def run_image_count_dist(cur, workspace_id: str) -> dict:
    """Image count histogram + flag ASINs with <5."""
    cur.execute(
        """
        SELECT asin,
               (ground_truth_fields->>'image_count')::int AS ic
        FROM asin_metadata
        WHERE workspace_id = %s
          AND ground_truth_fields->>'image_count' IS NOT NULL
          AND ground_truth_fields->>'image_count' ~ '^[0-9]+$'
        """,
        (workspace_id,),
    )
    rows = cur.fetchall()

    hist = {"0": 0, "1": 0, "2": 0, "3": 0, "4": 0, "5": 0, "6": 0, "7+": 0}
    under_5 = []
    under_7 = []
    for r in rows:
        asin, ic = r[0], int(r[1] or 0)
        bucket = str(ic) if ic <= 6 else "7+"
        hist[bucket] += 1
        if ic < 5:
            under_5.append((asin, ic))
        elif ic < 7:
            under_7.append((asin, ic))

    summary = {
        "total_measured": len(rows),
        "histogram": hist,
        "under_5_count": len(under_5),
        "under_7_count": len(under_7) + len(under_5),
    }

    findings = [
        _finding(
            "image_count_dist",
            "medium",
            0.5,
            evidence=summary,
            proposed_fix=(
                f"{len(under_5)} ASINs have fewer than 5 images (Amazon "
                f"recommends 7+). Prioritize shooting for the top-revenue "
                f"under-5 ASINs first."
            ),
        ),
    ]
    # Per-ASIN critical findings for <5
    for asin, ic in under_5[:500]:  # cap to 500 to keep findings volume sane
        findings.append(_finding(
            "fewer_than_5_images",
            "critical" if ic < 3 else "high",
            0.8 if ic < 3 else 0.6,
            asin=asin,
            evidence={"image_count": ic},
            proposed_fix=f"Add {5 - ic} images minimum (Amazon flagship recommendation is 7)",
        ))
    return {"summary": summary, "findings": findings}


def run_bullet_completeness_dist(cur, workspace_id: str) -> dict:
    """Histogram of bullet count per ASIN."""
    cur.execute(
        """
        SELECT asin,
               (CASE WHEN ground_truth_fields ? 'bullet_1' THEN 1 ELSE 0 END) +
               (CASE WHEN ground_truth_fields ? 'bullet_2' THEN 1 ELSE 0 END) +
               (CASE WHEN ground_truth_fields ? 'bullet_3' THEN 1 ELSE 0 END) +
               (CASE WHEN ground_truth_fields ? 'bullet_4' THEN 1 ELSE 0 END) +
               (CASE WHEN ground_truth_fields ? 'bullet_5' THEN 1 ELSE 0 END) AS n
        FROM asin_metadata
        WHERE workspace_id = %s
        """,
        (workspace_id,),
    )
    rows = cur.fetchall()
    hist = {"0": 0, "1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    under_5 = []
    for r in rows:
        n = int(r[1] or 0)
        hist[str(min(5, n))] += 1
        if n < 5:
            under_5.append((r[0], n))

    summary = {
        "total_measured": len(rows),
        "histogram": hist,
        "under_5_bullets": len(under_5),
        "with_all_5":     hist["5"],
    }
    findings = [
        _finding(
            "bullet_completeness_dist",
            "medium",
            0.4,
            evidence=summary,
            proposed_fix=(
                f"{len(under_5)} ASINs have fewer than 5 bullets. Amazon "
                f"indexes the first 200 chars of each; missing bullets = "
                f"missing search indexing."
            ),
        ),
    ]
    return {"summary": summary, "findings": findings}


def run_title_length_dist(cur, workspace_id: str) -> dict:
    """Histogram of title char count + flag <80 (weak SEO) and >200 (rejected)."""
    cur.execute(
        """
        SELECT asin,
               LENGTH(COALESCE(ground_truth_fields->>'title', '')) AS l
        FROM asin_metadata
        WHERE workspace_id = %s
        """,
        (workspace_id,),
    )
    rows = cur.fetchall()
    hist = {"0-40": 0, "41-80": 0, "81-120": 0,
            "121-160": 0, "161-200": 0, "201+": 0}
    under_80 = []
    over_200 = []
    for r in rows:
        l = int(r[1] or 0)
        if l <= 40:  hist["0-40"] += 1
        elif l <= 80:  hist["41-80"] += 1
        elif l <= 120: hist["81-120"] += 1
        elif l <= 160: hist["121-160"] += 1
        elif l <= 200: hist["161-200"] += 1
        else: hist["201+"] += 1
        if l < 80 and l > 0: under_80.append((r[0], l))
        if l > 200:          over_200.append((r[0], l))

    summary = {
        "total_measured": len(rows),
        "histogram": hist,
        "under_80_count": len(under_80),
        "over_200_count": len(over_200),
    }
    findings = [
        _finding(
            "title_length_dist",
            "medium",
            0.5,
            evidence=summary,
            proposed_fix=(
                f"{len(under_80)} titles are under 80 chars (weak SEO). "
                f"{len(over_200)} exceed 200 chars (Amazon will reject on "
                f"re-upload)."
            ),
        ),
    ]
    for asin, l in over_200[:200]:
        findings.append(_finding(
            "title_over_200_chars",
            "critical",
            0.9,
            asin=asin,
            evidence={"title_length": l},
            proposed_fix=f"Trim title to \u2264200 chars (currently {l})",
        ))
    return {"summary": summary, "findings": findings}


def run_dead_inventory(cur, workspace_id: str) -> dict:
    """ASINs with 0 sessions AND 0 units. Wrapped in one aggregate finding."""
    cur.execute(
        """
        SELECT am.asin
        FROM asin_metadata am
        LEFT JOIN (
            SELECT asin,
                   SUM(sessions) AS sessions,
                   SUM(units) AS units
            FROM asin_sales_metrics
            WHERE workspace_id = %s
            GROUP BY asin
        ) sm ON sm.asin = am.asin
        WHERE am.workspace_id = %s
          AND (sm.sessions IS NULL OR sm.sessions = 0)
          AND (sm.units IS NULL OR sm.units = 0)
        """,
        (workspace_id, workspace_id),
    )
    dead_asins = [r[0] for r in cur.fetchall()]

    cur.execute(
        "SELECT COUNT(*) FROM asin_metadata WHERE workspace_id = %s",
        (workspace_id,),
    )
    total = int(cur.fetchone()[0] or 0)

    summary = {
        "dead_count": len(dead_asins),
        "total_catalog": total,
        "dead_pct": round(100 * len(dead_asins) / total, 1) if total else 0,
        "sample": dead_asins[:20],
    }
    severity = "high" if len(dead_asins) > total * 0.5 else "medium"
    findings = [
        _finding(
            "dead_inventory",
            severity,
            0.7,
            evidence=summary,
            proposed_fix=(
                f"{len(dead_asins)} ASINs ({summary['dead_pct']}%) have zero "
                f"sessions AND zero units. Options: delist to reduce catalog "
                f"noise; consolidate into variation families; or invest in A+ "
                f"content + ads to test if content is the blocker."
            ),
        ),
    ]
    return {"summary": summary, "findings": findings}


def run_fill_rate_report(cur, workspace_id: str) -> dict:
    """Per-field fill rate. Always runnable; no per-ASIN findings."""
    cur.execute(
        """
        SELECT key, COUNT(*)
        FROM asin_metadata,
             LATERAL jsonb_object_keys(
               COALESCE(ground_truth_fields, '{}'::jsonb)
             ) AS key
        WHERE workspace_id = %s
        GROUP BY key
        """,
        (workspace_id,),
    )
    fields = {r[0]: int(r[1]) for r in cur.fetchall()}
    cur.execute(
        "SELECT COUNT(*) FROM asin_metadata WHERE workspace_id = %s",
        (workspace_id,),
    )
    total = int(cur.fetchone()[0] or 0)

    fill_pct = {k: round(100 * v / total, 1) if total else 0
                for k, v in fields.items()}
    summary = {"total_asins": total, "fill_pct": fill_pct}
    findings = [
        _finding("fill_rate_report", "strategic", 0.2,
                 evidence=summary,
                 proposed_fix="Fields <5% filled are effectively missing. "
                              "Fields 5-80% filled will produce partial-sample "
                              "analyses.")
    ]
    return {"summary": summary, "findings": findings}


# ============================================================
# v0.5 analyses
# ============================================================

def run_list_price_dist(cur, workspace_id: str) -> dict:
    """List-price histogram + outlier detection."""
    cur.execute(
        """
        SELECT asin, ground_truth_fields->>'list_price' AS lp
        FROM asin_metadata
        WHERE workspace_id = %s
          AND ground_truth_fields->>'list_price' IS NOT NULL
        """,
        (workspace_id,),
    )
    prices = []
    for r in cur.fetchall():
        try:
            p = float(r[1])
            if p > 0:
                prices.append((r[0], p))
        except (TypeError, ValueError):
            continue

    if not prices:
        return {"summary": {"n": 0}, "findings": []}

    vals = sorted(p for _, p in prices)
    n = len(vals)
    median = vals[n // 2]
    p10 = vals[max(0, n // 10)]
    p90 = vals[min(n - 1, n * 9 // 10)]
    p99 = vals[min(n - 1, n * 99 // 100)]
    minp, maxp = vals[0], vals[-1]

    # Histogram bands
    bands = [("$0-25", 0, 25), ("$25-50", 25, 50), ("$50-100", 50, 100),
             ("$100-200", 100, 200), ("$200-500", 200, 500),
             ("$500+", 500, float("inf"))]
    hist = {label: 0 for label, _, _ in bands}
    for v in vals:
        for label, lo, hi in bands:
            if lo <= v < hi:
                hist[label] += 1
                break

    # Outliers: > 3x p90 (upper) or < 0.3x p10 (lower)
    upper_cutoff = p90 * 3
    outliers = [(a, p) for a, p in prices if p > upper_cutoff][:20]

    summary = {
        "n": n, "min": round(minp, 2), "max": round(maxp, 2),
        "median": round(median, 2),
        "p10": round(p10, 2), "p90": round(p90, 2), "p99": round(p99, 2),
        "histogram": hist,
        "outlier_count": len(outliers),
    }
    findings = [
        _finding(
            "list_price_dist", "strategic", 0.3,
            evidence=summary,
            proposed_fix=(
                f"Prices range ${minp:.0f}–${maxp:.0f}, median ${median:.0f}. "
                f"P90 is ${p90:.0f}, top 1% at ${p99:.0f}+. "
                f"{len(outliers)} outliers above 3× P90 may be misconfigured."
            ),
        ),
    ]
    if outliers:
        findings[0]["evidence_json"]["outlier_sample"] = [
            {"asin": a, "price": p} for a, p in outliers[:10]
        ]
    return {"summary": summary, "findings": findings}


def run_subcategory_rollup(cur, workspace_id: str) -> dict:
    """Per-subcategory ASIN count, revenue, A+ coverage."""
    cur.execute(
        """
        WITH sales AS (
            SELECT asin, SUM(COALESCE(revenue, 0)) AS rev
            FROM asin_sales_metrics
            WHERE workspace_id = %s
            GROUP BY asin
        )
        SELECT
            COALESCE(am.ground_truth_fields->>'subcategory', '(none)') AS subcat,
            COUNT(DISTINCT am.asin) AS n,
            COALESCE(SUM(sales.rev), 0) AS rev,
            SUM(CASE WHEN LOWER(am.ground_truth_fields->>'a_plus_status') IN
                       ('yes','true','complete','published','1','y') THEN 1 ELSE 0 END) AS aplus_n
        FROM asin_metadata am
        LEFT JOIN sales ON sales.asin = am.asin
        WHERE am.workspace_id = %s
        GROUP BY subcat
        ORDER BY rev DESC
        """,
        (workspace_id, workspace_id),
    )
    rows = cur.fetchall()
    subcats = [{
        "subcategory": r[0],
        "asin_count": int(r[1]),
        "revenue": float(r[2] or 0),
        "a_plus_count": int(r[3]),
        "a_plus_pct": round(100 * int(r[3]) / int(r[1]), 1) if r[1] else 0,
    } for r in rows]

    total_rev = sum(s["revenue"] for s in subcats)
    for s in subcats:
        s["revenue_share_pct"] = round(
            100 * s["revenue"] / total_rev, 1) if total_rev else 0

    summary = {
        "n_subcategories": len(subcats),
        "top_5": subcats[:5],
        "all_subcategories": subcats[:30],  # cap for JSON size
    }
    findings = [
        _finding(
            "subcategory_rollup", "strategic", 0.4,
            evidence=summary,
            proposed_fix=(
                f"{len(subcats)} subcategories on record. Top 5 by revenue "
                f"generate ${sum(s['revenue'] for s in subcats[:5]):,.0f}. "
                f"Review A+ coverage per subcategory — low-coverage subcats "
                f"with high revenue are the biggest quick wins."
            ),
        ),
    ]
    return {"summary": summary, "findings": findings}


def run_style_family_concentration(cur, workspace_id: str) -> dict:
    """Children per parent. Orphans (childless parents), mega-clusters."""
    cur.execute(
        """
        SELECT parent_asin, COUNT(*) AS n
        FROM asin_metadata
        WHERE workspace_id = %s AND parent_asin IS NOT NULL
        GROUP BY parent_asin
        ORDER BY n DESC
        """,
        (workspace_id,),
    )
    fam_sizes = [(r[0], int(r[1])) for r in cur.fetchall()]

    # Parents that exist as their own row but have zero children
    cur.execute(
        """
        SELECT COUNT(*) FROM asin_metadata am
        WHERE am.workspace_id = %s
          AND am.parent_asin IS NULL
          AND NOT EXISTS (
            SELECT 1 FROM asin_metadata c
            WHERE c.workspace_id = am.workspace_id
              AND c.parent_asin = am.asin
          )
        """,
        (workspace_id,),
    )
    orphan_parents = int(cur.fetchone()[0] or 0)

    # Buckets
    single = sum(1 for _, n in fam_sizes if n == 1)
    small = sum(1 for _, n in fam_sizes if 2 <= n <= 5)
    medium = sum(1 for _, n in fam_sizes if 6 <= n <= 20)
    large = sum(1 for _, n in fam_sizes if 21 <= n <= 50)
    mega = sum(1 for _, n in fam_sizes if n > 50)

    mega_list = [(p, n) for p, n in fam_sizes if n > 50][:10]

    summary = {
        "total_families": len(fam_sizes),
        "single_child": single,
        "small_2_5": small,
        "medium_6_20": medium,
        "large_21_50": large,
        "mega_gt_50": mega,
        "orphan_parents": orphan_parents,
        "mega_sample": [{"parent": p, "children": n} for p, n in mega_list],
    }
    severity = "medium" if mega > 5 or orphan_parents > 10 else "low"
    findings = [
        _finding(
            "style_family_concentration", severity, 0.4,
            evidence=summary,
            proposed_fix=(
                f"{len(fam_sizes)} variation families. {mega} ‘mega’ "
                f"families with >50 children risk fragmented CVR (Amazon "
                f"shoppers can’t pick). {orphan_parents} parents have no "
                f"children on record."
            ),
        ),
    ]
    return {"summary": summary, "findings": findings}


def run_variation_theme_integrity(cur, workspace_id: str) -> dict:
    """Parents missing variation_theme; children with mismatched themes."""
    cur.execute(
        """
        SELECT am.asin,
               am.ground_truth_fields->>'variation_theme' AS theme,
               COUNT(c.asin) AS n_children,
               COUNT(DISTINCT c.ground_truth_fields->>'variation_theme')
                    AS distinct_child_themes
        FROM asin_metadata am
        LEFT JOIN asin_metadata c
          ON c.workspace_id = am.workspace_id
         AND c.parent_asin = am.asin
        WHERE am.workspace_id = %s
          AND am.parent_asin IS NULL
        GROUP BY am.asin, theme
        HAVING COUNT(c.asin) > 0
        """,
        (workspace_id,),
    )
    parents = cur.fetchall()
    missing_theme = [r[0] for r in parents if not r[1]]
    inconsistent = [r[0] for r in parents if int(r[3] or 0) > 1]

    summary = {
        "parents_with_children": len(parents),
        "missing_theme_count": len(missing_theme),
        "inconsistent_children_count": len(inconsistent),
        "missing_sample": missing_theme[:10],
        "inconsistent_sample": inconsistent[:10],
    }
    severity = "high" if (len(missing_theme) + len(inconsistent)) > 20 else "medium"
    findings = [
        _finding(
            "variation_theme_integrity", severity, 0.5,
            evidence=summary,
            proposed_fix=(
                f"{len(missing_theme)} parents have children but no "
                f"variation theme set. {len(inconsistent)} parents have "
                f"children with inconsistent themes (multiple values). "
                f"Both cause Amazon to break the variation family on the PDP."
            ),
        ),
    ]
    return {"summary": summary, "findings": findings}


def run_description_presence(cur, workspace_id: str) -> dict:
    """% of ASINs with description + length distribution."""
    cur.execute(
        """
        SELECT
            COUNT(*) FILTER (
                WHERE ground_truth_fields ? 'description'
                  AND LENGTH(ground_truth_fields->>'description') > 0
            ) AS with_desc,
            COUNT(*) AS total,
            AVG(LENGTH(ground_truth_fields->>'description'))
                FILTER (WHERE ground_truth_fields ? 'description')::int
                AS avg_len,
            COUNT(*) FILTER (
                WHERE LENGTH(ground_truth_fields->>'description') < 200
                  AND LENGTH(ground_truth_fields->>'description') > 0
            ) AS short_desc
        FROM asin_metadata
        WHERE workspace_id = %s
        """,
        (workspace_id,),
    )
    r = cur.fetchone()
    with_desc = int(r[0] or 0)
    total = int(r[1] or 0)
    avg_len = int(r[2] or 0) if r[2] is not None else 0
    short = int(r[3] or 0)
    missing = total - with_desc
    pct_with = round(100 * with_desc / total, 1) if total else 0

    # Sample ASINs missing descriptions (up to 20)
    cur.execute(
        """
        SELECT asin FROM asin_metadata
        WHERE workspace_id = %s
          AND (NOT (ground_truth_fields ? 'description')
               OR LENGTH(ground_truth_fields->>'description') = 0)
        LIMIT 20
        """,
        (workspace_id,),
    )
    no_desc_asins = [r[0] for r in cur.fetchall() if r[0]]

    summary = {
        "total": total,
        "with_description": with_desc,
        "missing_description": missing,
        "pct_with_description": pct_with,
        "avg_length_chars": avg_len,
        "short_descriptions_under_200": short,
        "no_desc_sample": no_desc_asins,
    }
    severity = "high" if pct_with < 70 else "medium"
    findings = [
        _finding(
            "description_presence", severity, 0.5,
            evidence=summary,
            proposed_fix=(
                f"{missing:,} of {total:,} ASINs ({100-pct_with:.0f}%) have "
                f"no description. Descriptions are not indexed for search but "
                f"drive conversion on the PDP. {short:,} descriptions are "
                f"under 200 chars (thin content)."
            ),
        ),
    ]
    return {"summary": summary, "findings": findings}


def run_buy_box_ownership(cur, workspace_id: str) -> dict:
    """% of ASINs where operator owns the buy box."""
    cur.execute(
        """
        SELECT
            COALESCE(ground_truth_fields->>'buy_box_winner', '(none)') AS winner,
            COUNT(*) AS n
        FROM asin_metadata
        WHERE workspace_id = %s
        GROUP BY winner
        ORDER BY n DESC
        """,
        (workspace_id,),
    )
    rows = cur.fetchall()
    dist = [{"winner": r[0], "count": int(r[1])} for r in rows]
    total = sum(d["count"] for d in dist)

    # Best guess: workspace's own brand name = the client. Look up in
    # brand_workspace or fall back to majority winner.
    likely_owner_pct = 0
    owner_name = None
    if dist:
        # If a single "winner" holds >50%, treat as the operator
        top = dist[0]
        if top["winner"] and top["winner"] != "(none)" and \
           100 * top["count"] / total > 50:
            owner_name = top["winner"]
            likely_owner_pct = round(100 * top["count"] / total, 1)

    # Sample ASINs where the operator does NOT own the buy box (up to 20)
    lost_buybox_asins: list = []
    if owner_name:
        cur.execute(
            """
            SELECT asin FROM asin_metadata
            WHERE workspace_id = %s
              AND COALESCE(ground_truth_fields->>'buy_box_winner', '') <> %s
              AND COALESCE(ground_truth_fields->>'buy_box_winner', '') <> ''
            LIMIT 20
            """,
            (workspace_id, owner_name),
        )
        lost_buybox_asins = [r[0] for r in cur.fetchall() if r[0]]

    summary = {
        "total": total,
        "distribution": dist[:10],
        "likely_owner": owner_name,
        "likely_owner_pct": likely_owner_pct,
        "lost_buybox_sample": lost_buybox_asins,
    }
    severity = "medium" if likely_owner_pct < 80 and total > 0 else "low"
    findings = [
        _finding(
            "buy_box_ownership", severity, 0.3,
            evidence=summary,
            proposed_fix=(
                f"Buy box is held by {owner_name or 'unknown'} on "
                f"{likely_owner_pct:.0f}% of ASINs. Losses to 3P sellers cost "
                f"revenue — audit price + FBA vs FBM on the losing ASINs."
                if likely_owner_pct else
                "Buy box ownership distribution is fragmented; no single "
                "seller dominates. Investigate whether 3P sellers are "
                "undercutting on top-revenue ASINs."
            ),
        ),
    ]
    return {"summary": summary, "findings": findings}


def run_fabric_material_coverage(cur, workspace_id: str) -> dict:
    """Apparel: % of ASINs with fabric/material populated."""
    cur.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE ground_truth_fields ? 'fabric_material'
                             AND LENGTH(ground_truth_fields->>'fabric_material') > 0) AS filled,
            COUNT(*) AS total
        FROM asin_metadata
        WHERE workspace_id = %s
        """,
        (workspace_id,),
    )
    r = cur.fetchone()
    filled = int(r[0] or 0)
    total = int(r[1] or 0)
    pct = round(100 * filled / total, 1) if total else 0

    # Sample ASINs missing fabric/material (up to 20)
    cur.execute(
        """
        SELECT asin FROM asin_metadata
        WHERE workspace_id = %s
          AND (NOT (ground_truth_fields ? 'fabric_material')
               OR LENGTH(ground_truth_fields->>'fabric_material') = 0)
        LIMIT 20
        """,
        (workspace_id,),
    )
    missing_fabric_asins = [r[0] for r in cur.fetchall() if r[0]]

    summary = {"total": total, "filled": filled, "pct_filled": pct,
               "missing_fabric_sample": missing_fabric_asins}
    findings = [
        _finding(
            "fabric_material_coverage",
            "low", 0.2,
            evidence=summary,
            proposed_fix=(
                f"{filled:,} of {total:,} ASINs ({pct:.0f}%) have fabric "
                f"composition set. Amazon requires this for apparel; missing "
                f"values risk suppression."
            ),
        ),
    ]
    return {"summary": summary, "findings": findings}


# ============================================================
# Runner
# ============================================================

ANALYSIS_FUNCS = [
    ("fill_rate_report",              run_fill_rate_report),
    ("concentration_pareto",          run_concentration_pareto),
    ("dead_inventory",                run_dead_inventory),
    ("cohort_split",                  run_cohort_split),
    ("a_plus_lift",                   run_a_plus_lift),
    ("image_count_dist",              run_image_count_dist),
    ("bullet_completeness_dist",      run_bullet_completeness_dist),
    ("title_length_dist",             run_title_length_dist),
    # v0.5
    ("list_price_dist",               run_list_price_dist),
    ("subcategory_rollup",            run_subcategory_rollup),
    ("style_family_concentration",    run_style_family_concentration),
    ("variation_theme_integrity",     run_variation_theme_integrity),
    ("description_presence",          run_description_presence),
    ("buy_box_ownership",             run_buy_box_ownership),
    ("fabric_material_coverage",      run_fabric_material_coverage),
]


# Known evidence-JSON keys that carry per-ASIN sample lists.
# Each analysis uses its own descriptive name (dead_asin_sample, outlier_sample,
# missing_sample, etc.) so operators reading the raw evidence understand it.
# We normalize into `sample_asins` (flat list of ASIN strings) for UI drilldown.
_SAMPLE_KEYS_FLAT = (
    "sample",
    "dead_asin_sample",
    "missing_sample",
    "inconsistent_sample",
    "no_desc_sample",
    "no_bullet_sample",
    "no_image_sample",
    "missing_fabric_sample",
    "lost_buybox_sample",
)
_SAMPLE_KEYS_DICT_ASIN = (
    "outlier_sample",       # [{asin, price}, ...]
)


def _normalize_sample_asins(evidence: dict) -> None:
    """In-place: add a `sample_asins` list to evidence for UI drilldown.

    Collects ASIN strings from known keys (flat lists or list-of-dicts with 'asin').
    Non-destructive: original keys are preserved. Skips if already present.
    """
    if not isinstance(evidence, dict):
        return
    if evidence.get("sample_asins"):
        return
    out: list = []
    seen: set = set()
    for k in _SAMPLE_KEYS_FLAT:
        v = evidence.get(k)
        if isinstance(v, list):
            for a in v:
                if isinstance(a, str) and a and a not in seen:
                    out.append(a); seen.add(a)
    for k in _SAMPLE_KEYS_DICT_ASIN:
        v = evidence.get(k)
        if isinstance(v, list):
            for row in v:
                if isinstance(row, dict):
                    a = row.get("asin")
                    if isinstance(a, str) and a and a not in seen:
                        out.append(a); seen.add(a)
    if out:
        evidence["sample_asins"] = out


def _attach_rule_definition(evidence: dict, rule_name: str) -> None:
    """In-place: attach a compact rule spec to each finding's evidence.

    Import here (not at module load) so the runner degrades gracefully if
    rules_catalog isn't deployed yet.
    """
    if not isinstance(evidence, dict):
        return
    try:
        from substrate.rules_catalog import get_rule_definition_for_finding
        evidence["rule_definition"] = get_rule_definition_for_finding(rule_name)
    except Exception:
        # Non-fatal: findings still write, just without inline rule spec
        pass


def run_all(workspace_id: str, *, snapshot_id: Optional[str] = None) -> dict:
    """Run every analysis and write findings.

    If snapshot_id is given, clears any prior findings for that snapshot
    first (idempotent). Otherwise clears workspace-wide findings.

    Returns:
      {ok, workspace_id, snapshot_id,
       analyses: [{id, ok, summary, findings_written}, ...],
       total_findings_written}
    """
    pool = get_pool()
    if pool is None:
        return {"ok": False, "error": "no db pool"}

    out_analyses = []
    total_written = 0
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # Clear prior findings for idempotency
                if snapshot_id:
                    cur.execute(
                        "DELETE FROM catalog_intel_findings WHERE snapshot_id = %s",
                        (snapshot_id,),
                    )
                else:
                    cur.execute(
                        "DELETE FROM catalog_intel_findings WHERE workspace_id = %s",
                        (workspace_id,),
                    )

                # Use a fresh cursor for each analysis (avoids nested-cursor issues)
                for aid, fn in ANALYSIS_FUNCS:
                    try:
                        with conn.cursor() as ac:
                            result = fn(ac, workspace_id)
                        findings = result.get("findings", [])
                        for f in findings:
                            ev = f.get("evidence_json") or {}
                            # Normalize sample ASINs for UI drilldown
                            _normalize_sample_asins(ev)
                            # Attach compact rule spec inline (verifiability layer)
                            _attach_rule_definition(ev, f.get("rule_name", aid))
                            fid = str(uuid.uuid4())
                            cur.execute(
                                """
                                INSERT INTO catalog_intel_findings
                                    (finding_id, snapshot_id, workspace_id, asin,
                                     rule_name, severity, priority_score,
                                     evidence_json, proposed_fix)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                                """,
                                (fid,
                                 snapshot_id or _placeholder_snapshot_id(),
                                 workspace_id, f.get("asin"),
                                 f["rule_name"], f["severity"],
                                 f["priority_score"],
                                 json.dumps(f["evidence_json"] or {}),
                                 f.get("proposed_fix")),
                            )
                            total_written += 1
                        out_analyses.append({
                            "id": aid,
                            "ok": True,
                            "summary": result.get("summary", {}),
                            "findings_written": len(findings),
                        })
                    except Exception as ax:
                        logger.exception("analysis %s failed", aid)
                        out_analyses.append({
                            "id": aid, "ok": False,
                            "error": str(ax)[:200],
                            "summary": {}, "findings_written": 0,
                        })
            conn.commit()
        return {
            "ok": True,
            "workspace_id": workspace_id,
            "snapshot_id": snapshot_id,
            "analyses": out_analyses,
            "total_findings_written": total_written,
        }
    except Exception as exc:
        logger.exception("run_all failed")
        return {"ok": False, "error": str(exc)[:200]}


# NIL uuid placeholder when we don't have a snapshot to attribute findings to
def _placeholder_snapshot_id() -> str:
    return "00000000-0000-0000-0000-000000000000"


def get_findings(
    workspace_id: str,
    *,
    snapshot_id: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """Return findings for a workspace (or snapshot), priority-sorted."""
    pool = get_pool()
    if pool is None:
        return []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                if snapshot_id:
                    cur.execute(
                        """
                        SELECT finding_id, snapshot_id, asin, rule_name,
                               severity, priority_score, evidence_json,
                               proposed_fix, created_at
                        FROM catalog_intel_findings
                        WHERE workspace_id = %s AND snapshot_id = %s
                        ORDER BY priority_score DESC NULLS LAST, created_at DESC
                        LIMIT %s
                        """,
                        (workspace_id, snapshot_id, int(limit)),
                    )
                else:
                    cur.execute(
                        """
                        SELECT finding_id, snapshot_id, asin, rule_name,
                               severity, priority_score, evidence_json,
                               proposed_fix, created_at
                        FROM catalog_intel_findings
                        WHERE workspace_id = %s
                        ORDER BY priority_score DESC NULLS LAST, created_at DESC
                        LIMIT %s
                        """,
                        (workspace_id, int(limit)),
                    )
                rows = cur.fetchall()
        return [{
            "finding_id":   str(r[0]),
            "snapshot_id":  str(r[1]) if r[1] else None,
            "asin":         r[2],
            "rule_name":    r[3],
            "severity":     r[4],
            "priority_score": float(r[5]) if r[5] is not None else None,
            "evidence":     r[6] or {},
            "proposed_fix": r[7],
            "created_at":   r[8].isoformat() if r[8] else None,
        } for r in rows]
    except Exception as exc:
        logger.warning("get_findings failed: %s", exc)
        return []
