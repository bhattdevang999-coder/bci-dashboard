"""Atlas substrate — catalog audit operational tables.

Bundles the writers for the five operational tables:
  - cohort_classifications
  - catalog_audit_findings
  - audit_decisions
  - audit_sessions
  - analytics_views

The rule library + brand registry live in their own modules
(substrate/audit_rules.py, substrate/brand_workspace.py).

Best-effort writes. Never raises.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.catalog_audit")


VALID_COHORTS = (
    "active", "latent_in_stock", "latent_unranked",
    "archive", "unknown",
)

VALID_QUEUES = (
    "quick_win", "content_quality", "strategic", "manual_review",
)

VALID_DECISION_ACTIONS = (
    "accept", "edit", "reject", "defer", "skip",
)


# ───────────────────────── cohort_classifications ─────────────────────────


def classify_cohort(
    workspace_id: str,
    asin: str,
    *,
    cohort: str,
    inputs_used: dict[str, Any],
    rule_applied: str,
    classified_by: str = "system",
) -> Optional[str]:
    """Append a classification row. Marks any prior current row as not-current."""
    if cohort not in VALID_COHORTS:
        logger.warning("classify_cohort: invalid cohort %s", cohort)
        return None
    pool = get_pool()
    if pool is None:
        return None
    cid = str(uuid.uuid4())
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # Demote prior current row
                cur.execute(
                    """
                    UPDATE cohort_classifications
                    SET is_current = false
                    WHERE workspace_id = %s AND asin = %s
                      AND is_current = true
                    """,
                    (workspace_id, asin),
                )
                cur.execute(
                    """
                    INSERT INTO cohort_classifications (
                        classification_id, workspace_id, asin,
                        cohort, inputs_used, rule_applied,
                        classified_by, is_current
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s::jsonb, %s,
                        %s, true
                    )
                    """,
                    (
                        cid, workspace_id, asin,
                        cohort, json.dumps(inputs_used), rule_applied,
                        classified_by,
                    ),
                )
            conn.commit()
        return cid
    except Exception as exc:
        logger.warning("classify_cohort failed: %s", exc)
        return None


def classify_cohort_bulk(
    workspace_id: str,
    rows: list[dict[str, Any]],
    classified_by: str = "system",
) -> int:
    """Bulk classify many ASINs at once. Each row must have keys:
    asin, cohort, inputs_used, rule_applied. Returns count written.

    Demotes prior current rows in a single update, then bulk-inserts new
    rows. Used during the audit run.
    """
    pool = get_pool()
    if pool is None or not rows:
        return 0
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                asins = [r["asin"] for r in rows]
                # Demote all in one update
                cur.execute(
                    """
                    UPDATE cohort_classifications
                    SET is_current = false
                    WHERE workspace_id = %s
                      AND asin = ANY(%s)
                      AND is_current = true
                    """,
                    (workspace_id, asins),
                )
                # Insert new rows
                values = []
                for r in rows:
                    if r["cohort"] not in VALID_COHORTS:
                        continue
                    values.append((
                        str(uuid.uuid4()), workspace_id, r["asin"],
                        r["cohort"],
                        json.dumps(r.get("inputs_used") or {}),
                        r["rule_applied"],
                        classified_by,
                    ))
                if values:
                    cur.executemany(
                        """
                        INSERT INTO cohort_classifications (
                            classification_id, workspace_id, asin,
                            cohort, inputs_used, rule_applied,
                            classified_by, is_current
                        ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, true)
                        """,
                        values,
                    )
            conn.commit()
            return len(values)
    except Exception as exc:
        logger.warning("classify_cohort_bulk failed: %s", exc)
        return 0


def get_cohort(workspace_id: str, asin: str) -> Optional[dict[str, Any]]:
    """Latest current classification for an ASIN, or None."""
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT classification_id, cohort, inputs_used,
                           rule_applied, classified_at, classified_by
                    FROM cohort_classifications
                    WHERE workspace_id = %s AND asin = %s
                      AND is_current = true
                    LIMIT 1
                    """,
                    (workspace_id, asin),
                )
                r = cur.fetchone()
                if not r:
                    return None
                return {
                    "classification_id": r[0],
                    "cohort": r[1],
                    "inputs_used": r[2] or {},
                    "rule_applied": r[3],
                    "classified_at": r[4].isoformat() if r[4] else None,
                    "classified_by": r[5],
                }
    except Exception as exc:
        logger.warning("get_cohort failed: %s", exc)
        return None


def count_by_cohort(workspace_id: str) -> dict[str, int]:
    """Counts by cohort label. Always returns the three canonical cohort keys
    (active, dormant, unknown) so callers can render coverage tiles without
    null-handling. Any extra cohort labels in the table are also returned.
    """
    out: dict[str, int] = {"active": 0, "dormant": 0, "unknown": 0}
    pool = get_pool()
    if pool is None:
        return out
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT cohort, COUNT(*)
                    FROM cohort_classifications
                    WHERE workspace_id = %s AND is_current = true
                    GROUP BY cohort
                    """,
                    (workspace_id,),
                )
                for row in cur.fetchall():
                    out[row[0]] = row[1]
                return out
    except Exception as exc:
        logger.warning("count_by_cohort failed: %s", exc)
        return out


# ───────────────────────── catalog_audit_findings ─────────────────────────


def write_finding(
    workspace_id: str,
    *,
    audit_run_id: str,
    asin: str,
    rule_id: str,
    rule_name: str,
    severity: str,
    revenue_exposure: Optional[float],
    evidence: dict[str, Any],
    proposed_fix: Optional[dict[str, Any]] = None,
    confidence: Optional[float] = None,
    queue: str = "manual_review",
    priority_score: Optional[float] = None,
    meta: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Append one finding row."""
    if queue not in VALID_QUEUES:
        queue = "manual_review"
    pool = get_pool()
    if pool is None:
        return None
    fid = str(uuid.uuid4())
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO catalog_audit_findings (
                        finding_id, workspace_id, audit_run_id,
                        asin, rule_id, rule_name,
                        severity, revenue_exposure,
                        evidence, proposed_fix,
                        confidence, queue, priority_score, meta
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s::jsonb, %s::jsonb,
                        %s, %s, %s, %s::jsonb
                    )
                    """,
                    (
                        fid, workspace_id, audit_run_id,
                        asin, rule_id, rule_name,
                        severity, revenue_exposure,
                        json.dumps(evidence),
                        json.dumps(proposed_fix) if proposed_fix else None,
                        confidence, queue, priority_score,
                        json.dumps(meta or {}),
                    ),
                )
            conn.commit()
        return fid
    except Exception as exc:
        logger.warning("write_finding failed: %s", exc)
        return None


def write_findings_bulk(
    workspace_id: str,
    audit_run_id: str,
    rows: list[dict[str, Any]],
) -> int:
    """Bulk write findings. Each row must have asin, rule_id, rule_name,
    severity, evidence; other fields optional. Returns count written."""
    pool = get_pool()
    if pool is None or not rows:
        return 0
    try:
        values = []
        for r in rows:
            queue = r.get("queue") or "manual_review"
            if queue not in VALID_QUEUES:
                queue = "manual_review"
            values.append((
                str(uuid.uuid4()), workspace_id, audit_run_id,
                r["asin"], r["rule_id"], r["rule_name"],
                r["severity"],
                r.get("revenue_exposure"),
                json.dumps(r["evidence"]),
                json.dumps(r.get("proposed_fix")) if r.get("proposed_fix") else None,
                r.get("confidence"),
                queue,
                r.get("priority_score"),
                json.dumps(r.get("meta") or {}),
            ))
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO catalog_audit_findings (
                        finding_id, workspace_id, audit_run_id,
                        asin, rule_id, rule_name,
                        severity, revenue_exposure,
                        evidence, proposed_fix,
                        confidence, queue, priority_score, meta
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s::jsonb, %s::jsonb,
                        %s, %s, %s, %s::jsonb
                    )
                    """,
                    values,
                )
            conn.commit()
        return len(values)
    except Exception as exc:
        logger.warning("write_findings_bulk failed: %s", exc)
        return 0


def list_findings(
    workspace_id: str,
    *,
    audit_run_id: Optional[str] = None,
    asin: Optional[str] = None,
    queue: Optional[str] = None,
    rule_name: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Findings sorted by priority score DESC."""
    pool = get_pool()
    if pool is None:
        return []
    where = ["workspace_id = %s"]
    params: list[Any] = [workspace_id]
    if audit_run_id:
        where.append("audit_run_id = %s")
        params.append(audit_run_id)
    if asin:
        where.append("asin = %s")
        params.append(asin)
    if queue:
        where.append("queue = %s")
        params.append(queue)
    if rule_name:
        where.append("rule_name = %s")
        params.append(rule_name)
    params.append(limit)
    sql = f"""
        SELECT finding_id, audit_run_id, asin, rule_id, rule_name,
               severity, revenue_exposure, evidence, proposed_fix,
               confidence, queue, priority_score,
               outcome_30d, outcome_60d, outcome_90d,
               created_at, meta
        FROM catalog_audit_findings
        WHERE {' AND '.join(where)}
        ORDER BY priority_score DESC NULLS LAST, created_at DESC
        LIMIT %s
    """
    out: list[dict[str, Any]] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                for r in cur.fetchall():
                    out.append({
                        "finding_id": r[0],
                        "audit_run_id": r[1],
                        "asin": r[2],
                        "rule_id": r[3],
                        "rule_name": r[4],
                        "severity": r[5],
                        "revenue_exposure":
                            float(r[6]) if r[6] is not None else None,
                        "evidence": r[7] or {},
                        "proposed_fix": r[8],
                        "confidence":
                            float(r[9]) if r[9] is not None else None,
                        "queue": r[10],
                        "priority_score":
                            float(r[11]) if r[11] is not None else None,
                        "outcome_30d": r[12],
                        "outcome_60d": r[13],
                        "outcome_90d": r[14],
                        "created_at":
                            r[15].isoformat() if r[15] else None,
                        "meta": r[16] or {},
                    })
    except Exception as exc:
        logger.warning("list_findings failed: %s", exc)
    return out


def get_finding(finding_id: str) -> Optional[dict[str, Any]]:
    rows = []
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT finding_id, workspace_id, audit_run_id, asin,
                           rule_id, rule_name, severity, revenue_exposure,
                           evidence, proposed_fix, confidence, queue,
                           priority_score, outcome_30d, outcome_60d,
                           outcome_90d, created_at, meta
                    FROM catalog_audit_findings
                    WHERE finding_id = %s
                    """,
                    (finding_id,),
                )
                r = cur.fetchone()
                if not r:
                    return None
                return {
                    "finding_id": r[0], "workspace_id": r[1],
                    "audit_run_id": r[2], "asin": r[3],
                    "rule_id": r[4], "rule_name": r[5],
                    "severity": r[6],
                    "revenue_exposure":
                        float(r[7]) if r[7] is not None else None,
                    "evidence": r[8] or {},
                    "proposed_fix": r[9],
                    "confidence":
                        float(r[10]) if r[10] is not None else None,
                    "queue": r[11],
                    "priority_score":
                        float(r[12]) if r[12] is not None else None,
                    "outcome_30d": r[13],
                    "outcome_60d": r[14],
                    "outcome_90d": r[15],
                    "created_at":
                        r[16].isoformat() if r[16] else None,
                    "meta": r[17] or {},
                }
    except Exception as exc:
        logger.warning("get_finding failed: %s", exc)
        return None


def attach_outcome_to_finding(
    finding_id: str,
    *,
    window: str,
    outcome: dict[str, Any],
) -> bool:
    """Attach a 30d / 60d / 90d outcome JSON to a finding."""
    col = f"outcome_{window}"
    if col not in ("outcome_30d", "outcome_60d", "outcome_90d"):
        return False
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE catalog_audit_findings
                    SET {col} = %s::jsonb
                    WHERE finding_id = %s
                    """,
                    (json.dumps(outcome), finding_id),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("attach_outcome_to_finding failed: %s", exc)
        return False


def summarize_run(
    workspace_id: str,
    audit_run_id: str,
) -> dict[str, Any]:
    """Roll-up stats for one audit run: counts by queue, severity, total
    revenue exposure."""
    pool = get_pool()
    if pool is None:
        return {}
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE queue='quick_win')
                          AS quick_wins,
                      COUNT(*) FILTER (WHERE queue='content_quality')
                          AS content_quality,
                      COUNT(*) FILTER (WHERE queue='strategic')
                          AS strategic,
                      COUNT(*) FILTER (WHERE queue='manual_review')
                          AS manual_review,
                      COUNT(*) FILTER (WHERE severity='critical')
                          AS critical,
                      COUNT(*) FILTER (WHERE severity='high')
                          AS high,
                      COUNT(*) FILTER (WHERE severity='medium')
                          AS medium,
                      COUNT(*) FILTER (WHERE severity='low')
                          AS low,
                      COUNT(*) FILTER (WHERE severity='strategic')
                          AS strategic_sev,
                      COALESCE(SUM(revenue_exposure), 0)
                          AS total_revenue_exposure,
                      COUNT(DISTINCT asin) AS asins_affected
                    FROM catalog_audit_findings
                    WHERE workspace_id = %s AND audit_run_id = %s
                    """,
                    (workspace_id, audit_run_id),
                )
                r = cur.fetchone()
                if not r:
                    return {}
                return {
                    "total": r[0] or 0,
                    "by_queue": {
                        "quick_win": r[1] or 0,
                        "content_quality": r[2] or 0,
                        "strategic": r[3] or 0,
                        "manual_review": r[4] or 0,
                    },
                    "by_severity": {
                        "critical": r[5] or 0,
                        "high": r[6] or 0,
                        "medium": r[7] or 0,
                        "low": r[8] or 0,
                        "strategic": r[9] or 0,
                    },
                    "total_revenue_exposure":
                        float(r[10]) if r[10] is not None else 0.0,
                    "asins_affected": r[11] or 0,
                }
    except Exception as exc:
        logger.warning("summarize_run failed: %s", exc)
        return {}


def top_issues_by_revenue(
    workspace_id: str,
    audit_run_id: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Rules sorted by total revenue exposure, descending."""
    pool = get_pool()
    if pool is None:
        return []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT rule_name,
                           COUNT(*) AS n_findings,
                           COALESCE(SUM(revenue_exposure), 0) AS total_rev,
                           AVG(confidence) AS avg_conf,
                           MAX(severity) AS severity_label
                    FROM catalog_audit_findings
                    WHERE workspace_id = %s AND audit_run_id = %s
                    GROUP BY rule_name
                    ORDER BY total_rev DESC NULLS LAST
                    LIMIT %s
                    """,
                    (workspace_id, audit_run_id, limit),
                )
                return [
                    {
                        "rule_name": r[0],
                        "n_findings": r[1],
                        "revenue_exposure":
                            float(r[2]) if r[2] is not None else 0.0,
                        "avg_confidence":
                            float(r[3]) if r[3] is not None else None,
                        "severity": r[4],
                    }
                    for r in cur.fetchall()
                ]
    except Exception as exc:
        logger.warning("top_issues_by_revenue failed: %s", exc)
        return []


# ───────────────────────── audit_decisions ─────────────────────────


def record_decision(
    finding_id: str,
    workspace_id: str,
    *,
    decision_action: str,
    decision_value: Optional[dict[str, Any]] = None,
    top_candidates_offered: Optional[list[dict[str, Any]]] = None,
    chosen_candidate: Optional[str] = None,
    time_to_first_action_ms: Optional[int] = None,
    time_dwelled_ms: Optional[int] = None,
    session_id: Optional[str] = None,
    decided_by: str = "devang",
) -> Optional[str]:
    """Capture an operator decision. Returns decision_id."""
    if decision_action not in VALID_DECISION_ACTIONS:
        logger.warning(
            "record_decision: invalid decision_action %s", decision_action
        )
        return None
    pool = get_pool()
    if pool is None:
        return None
    did = str(uuid.uuid4())
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_decisions (
                        decision_id, finding_id, workspace_id, session_id,
                        decision_action, decision_value,
                        top_candidates_offered, chosen_candidate,
                        time_to_first_action_ms, time_dwelled_ms,
                        decided_by
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s::jsonb,
                        %s::jsonb, %s,
                        %s, %s, %s
                    )
                    """,
                    (
                        did, finding_id, workspace_id, session_id,
                        decision_action,
                        json.dumps(decision_value) if decision_value else None,
                        json.dumps(top_candidates_offered or []),
                        chosen_candidate,
                        time_to_first_action_ms, time_dwelled_ms,
                        decided_by,
                    ),
                )
            conn.commit()
        return did
    except Exception as exc:
        logger.warning("record_decision failed: %s", exc)
        return None


def list_decisions_for_finding(finding_id: str) -> list[dict[str, Any]]:
    pool = get_pool()
    if pool is None:
        return []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT decision_id, decision_action, decision_value,
                           top_candidates_offered, chosen_candidate,
                           time_to_first_action_ms, time_dwelled_ms,
                           decided_at, decided_by
                    FROM audit_decisions
                    WHERE finding_id = %s
                    ORDER BY decided_at
                    """,
                    (finding_id,),
                )
                return [
                    {
                        "decision_id": r[0],
                        "decision_action": r[1],
                        "decision_value": r[2],
                        "top_candidates_offered": r[3] or [],
                        "chosen_candidate": r[4],
                        "time_to_first_action_ms": r[5],
                        "time_dwelled_ms": r[6],
                        "decided_at":
                            r[7].isoformat() if r[7] else None,
                        "decided_by": r[8],
                    }
                    for r in cur.fetchall()
                ]
    except Exception as exc:
        logger.warning("list_decisions_for_finding failed: %s", exc)
        return []


# ───────────────────────── audit_sessions ─────────────────────────


def start_session(
    workspace_id: str,
    *,
    operator_id: str = "devang",
) -> Optional[str]:
    """Open a new audit session."""
    pool = get_pool()
    if pool is None:
        return None
    sid = str(uuid.uuid4())
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_sessions (
                        session_id, workspace_id, operator_id
                    ) VALUES (%s, %s, %s)
                    """,
                    (sid, workspace_id, operator_id),
                )
            conn.commit()
        return sid
    except Exception as exc:
        logger.warning("start_session failed: %s", exc)
        return None


def close_session(
    session_id: str,
    summary_text: Optional[str] = None,
) -> bool:
    """Compute aggregate stats from this session's decisions and close
    it. Idempotent — re-closing recomputes."""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # Aggregate decisions for this session
                cur.execute(
                    """
                    SELECT
                      COUNT(DISTINCT finding_id),
                      COUNT(*) FILTER (WHERE decision_action='accept'),
                      COUNT(*) FILTER (WHERE decision_action='edit'),
                      COUNT(*) FILTER (WHERE decision_action='reject'),
                      COUNT(*) FILTER (WHERE decision_action='defer'),
                      COUNT(*) FILTER (WHERE decision_action='skip'),
                      AVG(time_dwelled_ms)
                    FROM audit_decisions
                    WHERE session_id = %s
                    """,
                    (session_id,),
                )
                agg = cur.fetchone() or (0, 0, 0, 0, 0, 0, None)
                asins, accepts, edits, rejects, defers, skips, avg_ms = agg

                # Top reject reasons (free-text in decision_value.reason)
                cur.execute(
                    """
                    SELECT decision_value->>'reason' AS reason, COUNT(*)
                    FROM audit_decisions
                    WHERE session_id = %s AND decision_action = 'reject'
                          AND decision_value->>'reason' IS NOT NULL
                    GROUP BY reason
                    ORDER BY COUNT(*) DESC
                    LIMIT 5
                    """,
                    (session_id,),
                )
                top_reject_reasons = [r[0] for r in cur.fetchall()]

                cur.execute(
                    """
                    UPDATE audit_sessions
                    SET ended_at = NOW(),
                        duration_seconds =
                            EXTRACT(EPOCH FROM (NOW() - started_at))::int,
                        asins_reviewed = %s,
                        accepts = %s, edits = %s, rejects = %s,
                        defers = %s, skips = %s,
                        avg_time_to_decide_ms = %s,
                        top_reject_reasons = %s,
                        summary_text = COALESCE(%s, summary_text)
                    WHERE session_id = %s
                    """,
                    (
                        asins or 0, accepts or 0, edits or 0,
                        rejects or 0, defers or 0, skips or 0,
                        int(avg_ms) if avg_ms is not None else None,
                        top_reject_reasons, summary_text, session_id,
                    ),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("close_session failed: %s", exc)
        return False


def get_session(session_id: str) -> Optional[dict[str, Any]]:
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT session_id, workspace_id, operator_id,
                           started_at, ended_at, duration_seconds,
                           asins_reviewed, accepts, edits, rejects,
                           defers, skips, avg_time_to_decide_ms,
                           queue_focus, top_reject_reasons, summary_text
                    FROM audit_sessions WHERE session_id = %s
                    """,
                    (session_id,),
                )
                r = cur.fetchone()
                if not r:
                    return None
                return {
                    "session_id": r[0], "workspace_id": r[1],
                    "operator_id": r[2],
                    "started_at": r[3].isoformat() if r[3] else None,
                    "ended_at": r[4].isoformat() if r[4] else None,
                    "duration_seconds": r[5],
                    "asins_reviewed": r[6],
                    "accepts": r[7], "edits": r[8],
                    "rejects": r[9], "defers": r[10], "skips": r[11],
                    "avg_time_to_decide_ms": r[12],
                    "queue_focus": r[13],
                    "top_reject_reasons": r[14] or [],
                    "summary_text": r[15],
                }
    except Exception as exc:
        logger.warning("get_session failed: %s", exc)
        return None


# ───────────────────────── analytics_views ─────────────────────────


def pin_view(
    workspace_id: str,
    *,
    view_name: str,
    slice_spec: dict[str, Any],
    operator_id: str = "devang",
) -> Optional[str]:
    """Save a slice as a named pinned view."""
    if not view_name or not slice_spec:
        return None
    pool = get_pool()
    if pool is None:
        return None
    vid = str(uuid.uuid4())
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO analytics_views (
                        view_id, workspace_id, operator_id,
                        view_name, slice_spec
                    ) VALUES (%s, %s, %s, %s, %s::jsonb)
                    """,
                    (vid, workspace_id, operator_id,
                     view_name, json.dumps(slice_spec)),
                )
            conn.commit()
        return vid
    except Exception as exc:
        logger.warning("pin_view failed: %s", exc)
        return None


def list_pinned_views(
    workspace_id: str,
    operator_id: str = "devang",
) -> list[dict[str, Any]]:
    pool = get_pool()
    if pool is None:
        return []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT view_id, view_name, slice_spec,
                           created_at, last_opened_at, open_count
                    FROM analytics_views
                    WHERE workspace_id = %s AND operator_id = %s
                    ORDER BY last_opened_at DESC NULLS LAST,
                             created_at DESC
                    """,
                    (workspace_id, operator_id),
                )
                return [
                    {
                        "view_id": r[0], "view_name": r[1],
                        "slice_spec": r[2] or {},
                        "created_at":
                            r[3].isoformat() if r[3] else None,
                        "last_opened_at":
                            r[4].isoformat() if r[4] else None,
                        "open_count": r[5],
                    }
                    for r in cur.fetchall()
                ]
    except Exception as exc:
        logger.warning("list_pinned_views failed: %s", exc)
        return []


def touch_view(view_id: str) -> bool:
    """Bump open_count and stamp last_opened_at."""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE analytics_views
                    SET open_count = open_count + 1,
                        last_opened_at = NOW()
                    WHERE view_id = %s
                    """,
                    (view_id,),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("touch_view failed: %s", exc)
        return False


__all__ = [
    # cohort_classifications
    "classify_cohort", "classify_cohort_bulk",
    "get_cohort", "count_by_cohort",
    # findings
    "write_finding", "write_findings_bulk",
    "list_findings", "get_finding",
    "attach_outcome_to_finding",
    "summarize_run", "top_issues_by_revenue",
    # decisions
    "record_decision", "list_decisions_for_finding",
    # sessions
    "start_session", "close_session", "get_session",
    # analytics_views
    "pin_view", "list_pinned_views", "touch_view",
    # constants
    "VALID_COHORTS", "VALID_QUEUES", "VALID_DECISION_ACTIONS",
]
