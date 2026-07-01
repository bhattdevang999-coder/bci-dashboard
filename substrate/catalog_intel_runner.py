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
# Runner
# ============================================================

ANALYSIS_FUNCS = [
    ("fill_rate_report",           run_fill_rate_report),
    ("concentration_pareto",       run_concentration_pareto),
    ("dead_inventory",             run_dead_inventory),
    ("cohort_split",               run_cohort_split),
    ("a_plus_lift",                run_a_plus_lift),
    ("image_count_dist",           run_image_count_dist),
    ("bullet_completeness_dist",   run_bullet_completeness_dist),
    ("title_length_dist",          run_title_length_dist),
]


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
