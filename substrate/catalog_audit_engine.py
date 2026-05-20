"""Atlas substrate — catalog audit engine.

Runs the resolved rule library against a workspace's substrate
(asin_metadata + cohort_classifications + outcome_events) and writes
findings to catalog_audit_findings.

Honest scope:
  - SQL-first. Each rule is a single COUNT/SELECT, not a Python loop.
  - Best-effort. A rule that can't run (missing column, missing connector)
    returns 0 findings and is recorded as 'skipped' in the run summary,
    never raises.
  - Revenue exposure = TTM revenue attributed to the offending ASIN(s).
    For group-level rules (duplicate_style_group, abandoned_subcategory)
    revenue_exposure is the TOTAL revenue across the group — that's the
    money at stake in the decision, not the lift if you fix it.
  - Confidence at this stage is the rule's confidence_default. Loop 1
    (operator-edit-pair preference model) will personalize this later.
  - Predicted lift is NOT computed in this pass. That's Loop 2
    (ASIN-level decision posterior). For now, proposed_fix.expected_lift_pct
    is the rule's prior; the audit decision UI will treat that as a hint,
    not a promise.

Output contract:
    run_audit(workspace_id, run_id=None, dry_run=False)
        -> dict with: run_id, rules_evaluated, rules_skipped, total_findings,
                      duration_seconds, findings_by_rule, errors

A 'skipped' rule is one whose required substrate column is < 5% filled
or which depends on a connector that isn't wired. We surface skip reasons
so the operator knows what's blocking which rule.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.catalog_audit_engine")

# Fill-rate floor — below this, a rule is skipped to avoid noise.
COLUMN_FILL_FLOOR = 0.05

# Queue routing per rule. quick_win = high-confidence, low-effort fix.
# content_quality = requires copy/photo work. strategic = requires
# operator judgment, not a one-click fix.
QUEUE_ROUTING = {
    "fewer_than_7_images":      "content_quality",
    "fewer_than_5_images":      "content_quality",
    "fewer_than_5_bullets":     "content_quality",
    "no_description":           "content_quality",
    "missing_a_plus_top_decile": "quick_win",
    "missing_a_plus":           "content_quality",
    "title_under_80_chars":     "content_quality",
    "title_over_200_chars":     "quick_win",
    "orphan_variation":         "strategic",
    "duplicate_style_group":    "strategic",
    "title_missing_brand":      "content_quality",
    "title_mostly_uppercase":   "quick_win",
    "missing_country_of_origin": "quick_win",
    "missing_care_instructions": "quick_win",
    "abandoned_subcategory":    "strategic",
}


# ───────────────────────── helpers ─────────────────────────


def _fetch_revenue_map(workspace_id: str) -> dict[str, float]:
    """ASIN -> TTM revenue. Cached for the run. Returns 0 for unknown ASINs."""
    pool = get_pool()
    if pool is None:
        return {}
    out: dict[str, float] = {}
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT asin, SUM(value)
                      FROM outcome_events
                     WHERE workspace_id = %s AND metric = 'revenue'
                       AND value IS NOT NULL
                     GROUP BY asin
                    """,
                    (workspace_id,),
                )
                for asin, rev in cur.fetchall():
                    out[asin] = float(rev or 0)
        return out
    except Exception as exc:
        logger.warning("_fetch_revenue_map failed: %s", exc)
        return out


def _fetch_active_cohort_asins(workspace_id: str) -> set[str]:
    """ASINs classified as active for this workspace."""
    pool = get_pool()
    if pool is None:
        return set()
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT asin FROM cohort_classifications
                     WHERE workspace_id = %s AND cohort = 'active'
                       AND is_current = true
                    """,
                    (workspace_id,),
                )
                return {r[0] for r in cur.fetchall()}
    except Exception as exc:
        logger.warning("_fetch_active_cohort_asins failed: %s", exc)
        return set()


def _fetch_revenue_decile_top(
    workspace_id: str,
    decile_pct: float = 10.0,
) -> set[str]:
    """ASINs in the top revenue decile (default top 10%)."""
    rev_map = _fetch_revenue_map(workspace_id)
    if not rev_map:
        return set()
    sorted_asins = sorted(rev_map.items(), key=lambda x: x[1], reverse=True)
    cutoff = max(1, int(len(sorted_asins) * decile_pct / 100.0))
    return {a for a, _ in sorted_asins[:cutoff]}


def _column_fill_rate(workspace_id: str, key: str) -> float:
    """Fraction of asin_metadata rows where ground_truth_fields->>key is non-null
    and non-empty. Used to gate rules that need a column not in this XLSX."""
    pool = get_pool()
    if pool is None:
        return 0.0
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      COUNT(*) FILTER (
                        WHERE ground_truth_fields ? %s
                          AND COALESCE(NULLIF(TRIM(ground_truth_fields->>%s), ''),
                                       NULL) IS NOT NULL
                      )::float / NULLIF(COUNT(*), 0)
                    FROM asin_metadata WHERE workspace_id = %s
                    """,
                    (key, key, workspace_id),
                )
                r = cur.fetchone()
                return float(r[0] or 0.0) if r else 0.0
    except Exception as exc:
        logger.warning("_column_fill_rate(%s) failed: %s", key, exc)
        return 0.0


def _proposed_fix(rule_name: str, threshold: dict[str, Any],
                  rule: dict[str, Any]) -> dict[str, Any]:
    """Default proposed_fix payload per rule. Operator can edit."""
    return {
        "action_type": rule_name,
        "details": {"threshold": threshold},
        "expected_lift_pct": None,  # Loop 2 fills this
        "confidence": rule.get("confidence_default"),
        "queue": QUEUE_ROUTING.get(rule_name, "manual_review"),
        "model": rule.get("predicted_lift_model"),
    }


def _priority_score(severity: str, revenue: Optional[float],
                    confidence: Optional[float]) -> float:
    """Sort key for the operator queue. Higher = act sooner.

    severity_weight * log10(1 + revenue) * confidence
    """
    sw = {"critical": 4.0, "high": 3.0, "medium": 2.0,
          "low": 1.0, "strategic": 2.5}.get(severity, 1.0)
    import math
    rw = math.log10(1.0 + (revenue or 0.0))
    cw = confidence or 0.5
    return sw * rw * cw


# ───────────────────────── rule runners ─────────────────────────
#
# Each runner returns list[dict] of finding rows. Empty list = rule fired
# but no offenders found. Returning None = rule could not fire (skipped).
#
# Shared inputs: workspace_id, rule (resolved dict from audit_rules),
#                revenue_map, active_asins.


def _run_numeric_threshold_on_field(
    workspace_id: str, rule: dict, revenue_map: dict[str, float],
    active_asins: set[str], field: str, op: str, threshold_value: Any,
    cohort: str = "active",
) -> Optional[list[dict]]:
    """Generic runner for numeric_threshold over a ground_truth_fields key."""
    if _column_fill_rate(workspace_id, field) < COLUMN_FILL_FLOOR:
        return None

    pool = get_pool()
    if pool is None:
        return []

    sql_op = {"<": "<", ">": ">", "<=": "<=", ">=": ">=",
              "==": "=", "!=": "!="}.get(op)
    if not sql_op:
        logger.warning("unsupported op %s for rule %s", op, rule["name"])
        return None

    sql = f"""
        SELECT asin, (ground_truth_fields->>%s)::numeric AS actual
          FROM asin_metadata
         WHERE workspace_id = %s
           AND ground_truth_fields ? %s
           AND (ground_truth_fields->>%s) ~ '^-?[0-9]+(\\.[0-9]+)?$'
           AND (ground_truth_fields->>%s)::numeric {sql_op} %s
    """
    findings: list[dict] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (field, workspace_id, field, field,
                                  field, threshold_value))
                for asin, actual in cur.fetchall():
                    if cohort == "active" and asin not in active_asins:
                        continue
                    rev = revenue_map.get(asin, 0.0)
                    findings.append({
                        "asin": asin,
                        "rule_id": rule["rule_id"],
                        "rule_name": rule["name"],
                        "severity": rule["severity"],
                        "revenue_exposure": rev,
                        "confidence": rule["confidence_default"],
                        "queue": QUEUE_ROUTING.get(rule["name"], "manual_review"),
                        "evidence": {
                            "column": field,
                            "actual": float(actual),
                            "op": op,
                            "threshold": threshold_value,
                        },
                        "proposed_fix": _proposed_fix(
                            rule["name"], rule["threshold_json"], rule,
                        ),
                        "priority_score": _priority_score(
                            rule["severity"], rev, rule["confidence_default"],
                        ),
                    })
        return findings
    except Exception as exc:
        logger.warning("numeric_threshold rule %s failed: %s",
                       rule["name"], exc)
        return None


def _run_presence_check(
    workspace_id: str, rule: dict, revenue_map: dict[str, float],
    active_asins: set[str], field: str, expect: str,
    cohort: str = "active",
) -> Optional[list[dict]]:
    """Generic presence_check runner.

    expect in {'non_empty', 'Yes', 'No', ...}.
    For 'non_empty': fires if the column is missing or empty.
    For literal values: fires if value != expected.
    """
    if _column_fill_rate(workspace_id, field) < COLUMN_FILL_FLOOR \
       and expect == "non_empty":
        return None

    pool = get_pool()
    if pool is None:
        return []

    if expect == "non_empty":
        where = (
            "ground_truth_fields IS NULL OR NOT (ground_truth_fields ? %s) "
            "OR COALESCE(TRIM(ground_truth_fields->>%s), '') = ''"
        )
        params = (field, field, workspace_id)
    elif expect == "contains_brand":
        # Special-case: title must contain brand (substring, case-insensitive)
        where = (
            "ground_truth_fields ? 'title' AND ground_truth_fields ? 'brand' "
            "AND POSITION(LOWER(TRIM(ground_truth_fields->>'brand')) "
            "IN LOWER(ground_truth_fields->>'title')) = 0"
        )
        params = (workspace_id,)
    else:
        where = ("COALESCE(ground_truth_fields->>%s, '') <> %s")
        params = (field, expect, workspace_id)

    sql = f"""
        SELECT asin, ground_truth_fields->>%s AS actual
          FROM asin_metadata
         WHERE workspace_id = %s
           AND ({where})
    """
    # Build final params: SELECT field, workspace, then WHERE params
    final_params: tuple = (field, workspace_id) + (
        () if expect == "contains_brand"
        else (field, field) if expect == "non_empty"
        else (field, expect)
    )

    findings: list[dict] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, final_params)
                for asin, actual in cur.fetchall():
                    if cohort == "active" and asin not in active_asins:
                        continue
                    if not asin or asin in ("Required", "ASIN"):
                        # Skip the XLSX header row that leaked in
                        continue
                    rev = revenue_map.get(asin, 0.0)
                    findings.append({
                        "asin": asin,
                        "rule_id": rule["rule_id"],
                        "rule_name": rule["name"],
                        "severity": rule["severity"],
                        "revenue_exposure": rev,
                        "confidence": rule["confidence_default"],
                        "queue": QUEUE_ROUTING.get(rule["name"], "manual_review"),
                        "evidence": {
                            "column": field,
                            "actual": (actual or "")[:200],
                            "expected": expect,
                        },
                        "proposed_fix": _proposed_fix(
                            rule["name"], rule["threshold_json"], rule,
                        ),
                        "priority_score": _priority_score(
                            rule["severity"], rev, rule["confidence_default"],
                        ),
                    })
        return findings
    except Exception as exc:
        logger.warning("presence_check rule %s failed: %s",
                       rule["name"], exc)
        return None


def _run_orphan_variation(
    workspace_id: str, rule: dict, revenue_map: dict[str, float],
    active_asins: set[str],
) -> Optional[list[dict]]:
    """color_name AND size set BUT variation_theme blank."""
    if (_column_fill_rate(workspace_id, "color_name") < COLUMN_FILL_FLOOR
        or _column_fill_rate(workspace_id, "size") < COLUMN_FILL_FLOOR):
        return None
    pool = get_pool()
    if pool is None:
        return []
    sql = """
        SELECT asin,
               ground_truth_fields->>'color_name' AS color,
               ground_truth_fields->>'size' AS size,
               COALESCE(ground_truth_fields->>'variation_theme', '') AS vtheme
          FROM asin_metadata
         WHERE workspace_id = %s
           AND COALESCE(TRIM(ground_truth_fields->>'color_name'), '') <> ''
           AND COALESCE(TRIM(ground_truth_fields->>'size'), '') <> ''
           AND COALESCE(TRIM(ground_truth_fields->>'variation_theme'), '') = ''
    """
    findings: list[dict] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (workspace_id,))
                for asin, color, size, vtheme in cur.fetchall():
                    if asin not in active_asins:
                        continue
                    rev = revenue_map.get(asin, 0.0)
                    findings.append({
                        "asin": asin,
                        "rule_id": rule["rule_id"],
                        "rule_name": rule["name"],
                        "severity": rule["severity"],
                        "revenue_exposure": rev,
                        "confidence": rule["confidence_default"],
                        "queue": QUEUE_ROUTING[rule["name"]],
                        "evidence": {
                            "color": color, "size": size,
                            "variation_theme": vtheme or "(empty)",
                        },
                        "proposed_fix": _proposed_fix(
                            rule["name"], rule["threshold_json"], rule,
                        ),
                        "priority_score": _priority_score(
                            rule["severity"], rev, rule["confidence_default"],
                        ),
                    })
        return findings
    except Exception as exc:
        logger.warning("orphan_variation failed: %s", exc)
        return None


def _run_missing_a_plus_top_decile(
    workspace_id: str, rule: dict, revenue_map: dict[str, float],
    active_asins: set[str],
) -> Optional[list[dict]]:
    """a_plus_status=No AND in top revenue decile."""
    if _column_fill_rate(workspace_id, "a_plus_status") < COLUMN_FILL_FLOOR:
        return None
    top_decile = _fetch_revenue_decile_top(workspace_id, decile_pct=10.0)
    if not top_decile:
        return []
    pool = get_pool()
    if pool is None:
        return []
    sql = """
        SELECT asin
          FROM asin_metadata
         WHERE workspace_id = %s
           AND ground_truth_fields->>'a_plus_status' = 'No'
    """
    findings: list[dict] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (workspace_id,))
                hits = [r[0] for r in cur.fetchall()]
        for asin in hits:
            if asin not in top_decile:
                continue
            if asin not in active_asins:
                continue
            rev = revenue_map.get(asin, 0.0)
            findings.append({
                "asin": asin,
                "rule_id": rule["rule_id"],
                "rule_name": rule["name"],
                "severity": rule["severity"],
                "revenue_exposure": rev,
                "confidence": rule["confidence_default"],
                "queue": QUEUE_ROUTING[rule["name"]],
                "evidence": {
                    "a_plus_status": "No",
                    "decile": "top",
                    "rev_ttm": rev,
                },
                "proposed_fix": _proposed_fix(
                    rule["name"], rule["threshold_json"], rule,
                ),
                "priority_score": _priority_score(
                    rule["severity"], rev, rule["confidence_default"],
                ),
            })
        return findings
    except Exception as exc:
        logger.warning("missing_a_plus_top_decile failed: %s", exc)
        return None


def _run_duplicate_style_group(
    workspace_id: str, rule: dict, revenue_map: dict[str, float],
    active_asins: set[str],
) -> Optional[list[dict]]:
    """ASINs in style_number groups with >threshold members.

    One finding per ASIN. revenue_exposure = total revenue across the
    style cluster (the money at risk in the dedup decision).
    """
    threshold = rule["threshold_json"].get("value", 5)
    if _column_fill_rate(workspace_id, "style_number") < COLUMN_FILL_FLOOR:
        return None
    pool = get_pool()
    if pool is None:
        return []
    sql = """
        WITH grouped AS (
          SELECT ground_truth_fields->>'style_number' AS style, asin
            FROM asin_metadata
           WHERE workspace_id = %s
             AND COALESCE(TRIM(ground_truth_fields->>'style_number'), '') <> ''
        ),
        sized AS (
          SELECT style, COUNT(*) AS n
            FROM grouped GROUP BY style HAVING COUNT(*) > %s
        )
        SELECT g.asin, g.style, s.n
          FROM grouped g JOIN sized s USING (style)
    """
    findings: list[dict] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (workspace_id, threshold))
                rows = cur.fetchall()
        # Compute total revenue per style group for context
        style_rev: dict[str, float] = {}
        for asin, style, _n in rows:
            style_rev[style] = style_rev.get(style, 0.0) + revenue_map.get(asin, 0.0)
        for asin, style, n in rows:
            rev = revenue_map.get(asin, 0.0)
            group_rev = style_rev.get(style, 0.0)
            findings.append({
                "asin": asin,
                "rule_id": rule["rule_id"],
                "rule_name": rule["name"],
                "severity": rule["severity"],
                "revenue_exposure": group_rev,
                "confidence": rule["confidence_default"],
                "queue": QUEUE_ROUTING[rule["name"]],
                "evidence": {
                    "style_number": style,
                    "cluster_size": int(n),
                    "asin_rev_ttm": rev,
                    "cluster_rev_ttm": group_rev,
                },
                "proposed_fix": _proposed_fix(
                    rule["name"], rule["threshold_json"], rule,
                ),
                "priority_score": _priority_score(
                    rule["severity"], group_rev, rule["confidence_default"],
                ),
            })
        return findings
    except Exception as exc:
        logger.warning("duplicate_style_group failed: %s", exc)
        return None


def _run_title_mostly_uppercase(
    workspace_id: str, rule: dict, revenue_map: dict[str, float],
    active_asins: set[str],
) -> Optional[list[dict]]:
    """Compute uppercase ratio in Python — Postgres regex is awkward for this."""
    pool = get_pool()
    if pool is None:
        return []
    threshold = float(rule["threshold_json"].get("value", 0.5))
    findings: list[dict] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT asin, ground_truth_fields->>'title' AS title
                      FROM asin_metadata
                     WHERE workspace_id = %s
                       AND COALESCE(TRIM(ground_truth_fields->>'title'), '') <> ''
                    """,
                    (workspace_id,),
                )
                for asin, title in cur.fetchall():
                    if not title:
                        continue
                    letters = [c for c in title if c.isalpha()]
                    if not letters:
                        continue
                    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
                    if upper_ratio <= threshold:
                        continue
                    rev = revenue_map.get(asin, 0.0)
                    findings.append({
                        "asin": asin,
                        "rule_id": rule["rule_id"],
                        "rule_name": rule["name"],
                        "severity": rule["severity"],
                        "revenue_exposure": rev,
                        "confidence": rule["confidence_default"],
                        "queue": QUEUE_ROUTING[rule["name"]],
                        "evidence": {
                            "title": title[:120],
                            "uppercase_ratio": round(upper_ratio, 3),
                            "threshold": threshold,
                        },
                        "proposed_fix": _proposed_fix(
                            rule["name"], rule["threshold_json"], rule,
                        ),
                        "priority_score": _priority_score(
                            rule["severity"], rev, rule["confidence_default"],
                        ),
                    })
        return findings
    except Exception as exc:
        logger.warning("title_mostly_uppercase failed: %s", exc)
        return None


def _run_abandoned_subcategory(
    workspace_id: str, rule: dict, revenue_map: dict[str, float],
    active_asins: set[str],
) -> Optional[list[dict]]:
    """Subcategory aggregate: <20% A+ AND mean rev/listing <$50.

    Returns one finding per offending subcategory (the asin field carries
    a synthetic '__group__:<subcategory>' marker so the finding still
    satisfies the NOT NULL constraint).
    """
    if (_column_fill_rate(workspace_id, "subcategory") < COLUMN_FILL_FLOOR
        or _column_fill_rate(workspace_id, "a_plus_status") < COLUMN_FILL_FLOOR):
        return None
    a_plus_thresh = float(rule["threshold_json"]["all_of"][0]["value"])
    rev_thresh = float(rule["threshold_json"]["all_of"][1]["value"])

    pool = get_pool()
    if pool is None:
        return []
    sql = """
        SELECT ground_truth_fields->>'subcategory' AS subcat,
               COUNT(*) AS listings,
               SUM(CASE WHEN ground_truth_fields->>'a_plus_status'='Yes'
                        THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*),0) * 100
                 AS aplus_pct
          FROM asin_metadata
         WHERE workspace_id = %s
           AND COALESCE(TRIM(ground_truth_fields->>'subcategory'), '') <> ''
         GROUP BY subcat
         HAVING SUM(CASE WHEN ground_truth_fields->>'a_plus_status'='Yes'
                         THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*),0) * 100
                  < %s
    """
    findings: list[dict] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (workspace_id, a_plus_thresh))
                candidates = cur.fetchall()
            # For each candidate subcat, compute mean rev/listing
            for subcat, listings, aplus_pct in candidates:
                with conn.cursor() as cur2:
                    cur2.execute(
                        """
                        SELECT COALESCE(SUM(value), 0)
                          FROM outcome_events oe
                          JOIN asin_metadata am ON am.asin = oe.asin
                                                AND am.workspace_id = oe.workspace_id
                         WHERE oe.workspace_id = %s
                           AND oe.metric = 'revenue'
                           AND am.ground_truth_fields->>'subcategory' = %s
                        """,
                        (workspace_id, subcat),
                    )
                    total_rev = float(cur2.fetchone()[0] or 0)
                mean_rev = total_rev / max(1, listings)
                if mean_rev >= rev_thresh:
                    continue
                # Synthetic asin so we satisfy NOT NULL
                findings.append({
                    "asin": f"__group__:{subcat}"[:50],
                    "rule_id": rule["rule_id"],
                    "rule_name": rule["name"],
                    "severity": rule["severity"],
                    "revenue_exposure": total_rev,
                    "confidence": rule["confidence_default"],
                    "queue": QUEUE_ROUTING[rule["name"]],
                    "evidence": {
                        "subcategory": subcat,
                        "listings": int(listings),
                        "aplus_pct": round(float(aplus_pct), 1),
                        "mean_rev_per_listing": round(mean_rev, 2),
                        "total_rev_ttm": round(total_rev, 2),
                        "threshold_aplus_pct": a_plus_thresh,
                        "threshold_mean_rev": rev_thresh,
                    },
                    "proposed_fix": _proposed_fix(
                        rule["name"], rule["threshold_json"], rule,
                    ),
                    "priority_score": _priority_score(
                        rule["severity"], total_rev, rule["confidence_default"],
                    ),
                })
        return findings
    except Exception as exc:
        logger.warning("abandoned_subcategory failed: %s", exc)
        return None


# ───────────────────────── dispatch ─────────────────────────


def _dispatch(rule: dict, workspace_id: str,
              revenue_map: dict[str, float],
              active_asins: set[str]) -> Optional[list[dict]]:
    """Route a rule to its runner. Returns None if the rule cannot fire."""
    name = rule["name"]
    threshold = rule["threshold_json"]
    cohort = rule.get("applies_to", {}).get("cohort", "active")

    # Specialized rules
    if name == "orphan_variation":
        return _run_orphan_variation(workspace_id, rule, revenue_map, active_asins)
    if name == "missing_a_plus_top_decile":
        return _run_missing_a_plus_top_decile(
            workspace_id, rule, revenue_map, active_asins,
        )
    if name == "duplicate_style_group":
        return _run_duplicate_style_group(
            workspace_id, rule, revenue_map, active_asins,
        )
    if name == "title_mostly_uppercase":
        return _run_title_mostly_uppercase(
            workspace_id, rule, revenue_map, active_asins,
        )
    if name == "abandoned_subcategory":
        return _run_abandoned_subcategory(
            workspace_id, rule, revenue_map, active_asins,
        )

    # Generic numeric_threshold (simple key/op/value)
    if rule["rule_kind"] == "numeric_threshold":
        col = threshold.get("column")
        op = threshold.get("op")
        val = threshold.get("value")
        if not col or not op:
            return None
        # Some columns are named differently in the substrate
        field_map = {
            "image_count": "image_count",
            "bullet_count": "bullet_count",
            "title_length": "title_length",
        }
        field = field_map.get(col, col)
        return _run_numeric_threshold_on_field(
            workspace_id, rule, revenue_map, active_asins,
            field=field, op=op, threshold_value=val, cohort=cohort,
        )

    # Generic presence_check
    if rule["rule_kind"] == "presence_check":
        col = threshold.get("column")
        expect = threshold.get("expect")
        if not col or not expect:
            return None
        # Description: store 'description' but also has 'description_present' bool;
        # use the literal field 'description' for non_empty check
        return _run_presence_check(
            workspace_id, rule, revenue_map, active_asins,
            field=col, expect=expect, cohort=cohort,
        )

    # Unhandled rule kind
    return None


# ───────────────────────── entry point ─────────────────────────


def run_audit(workspace_id: str,
              run_id: Optional[str] = None,
              dry_run: bool = False) -> dict[str, Any]:
    """Run every resolved rule against the workspace substrate.

    Returns a summary dict. Writes findings to catalog_audit_findings
    unless dry_run=True (useful for the CLI report).
    """
    from . import audit_rules as ar
    from . import catalog_audit as ca

    run_id = run_id or str(uuid.uuid4())
    started = time.time()

    rules = ar.resolve_rules_for_brand(workspace_id)
    # Filter to active rules only
    rules = [r for r in rules if r.get("is_active", True)]
    revenue_map = _fetch_revenue_map(workspace_id)
    active_asins = _fetch_active_cohort_asins(workspace_id)

    findings_by_rule: dict[str, int] = {}
    skipped_rules: list[dict[str, str]] = []
    revenue_by_rule: dict[str, float] = {}
    all_findings: list[dict] = []
    errors: list[str] = []

    for rule in rules:
        try:
            rows = _dispatch(rule, workspace_id, revenue_map, active_asins)
        except Exception as exc:
            errors.append(f"{rule['name']}: {exc}")
            skipped_rules.append({"name": rule["name"],
                                  "reason": f"runner error: {exc}"})
            continue
        if rows is None:
            skipped_rules.append({
                "name": rule["name"],
                "reason": "required column < 5% filled or no connector",
            })
            findings_by_rule[rule["name"]] = 0
            continue
        findings_by_rule[rule["name"]] = len(rows)
        revenue_by_rule[rule["name"]] = sum(
            r["revenue_exposure"] or 0 for r in rows
        )
        all_findings.extend(rows)

    written = 0
    if not dry_run and all_findings:
        written = ca.write_findings_bulk(workspace_id, run_id, all_findings)

    duration = time.time() - started
    return {
        "run_id": run_id,
        "workspace_id": workspace_id,
        "rules_total": len(rules),
        "rules_evaluated": len(rules) - len(skipped_rules),
        "rules_skipped": len(skipped_rules),
        "skipped": skipped_rules,
        "findings_by_rule": findings_by_rule,
        "revenue_by_rule": revenue_by_rule,
        "total_findings": len(all_findings),
        "findings_written": written,
        "duration_seconds": round(duration, 2),
        "errors": errors,
        "active_cohort_size": len(active_asins),
        "total_revenue_ttm": sum(revenue_map.values()),
    }


__all__ = ["run_audit"]
