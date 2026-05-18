"""Atlas substrate — atlas_evaluation.

Per-field verdict rows attached to a recommendation_ingest. Carries the
5-layer citation chain, agency response (via tokenized link), and the
operator's final decision + applied value.

Contract:
    create_evaluation(...)          -> str | None
    list_evaluations(...)           -> list[dict]
    get_evaluation(...)             -> dict | None
    apply_agency_response(...)      -> bool
    apply_operator_decision(...)    -> bool
    summarize_rec(...)              -> dict  (counts by verdict + status)

Best-effort writes. Never raises.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.atlas_evaluation")


VALID_FIELD_OWNERS = (
    "manufacturer", "agency", "amazon_taxonomy",
    "operator_strategic", "atlas_calibrated", "ambiguous",
)

VALID_VERDICTS = ("agree", "partial", "disagree", "unknown")

VALID_CRITICALITIES = ("launch_blocking", "high", "normal", "low")

VALID_OPERATOR_DECISIONS = ("accept", "override", "defer", "reject")


def create_evaluation(
    rec_id: str,
    workspace_id: str,
    *,
    field_name: str,
    submitted_value: Optional[str],
    field_owner: str,
    verdict: str,
    reasoning: str,
    citations: Optional[list[dict[str, Any]]] = None,
    proposed_alternative: Optional[str] = None,
    test_design: Optional[str] = None,
    evidence_path: Optional[str] = None,
    confidence: Optional[float] = None,
    criticality: str = "normal",
    meta: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Append one atlas_evaluation row. Returns eval_id."""
    if field_owner not in VALID_FIELD_OWNERS:
        logger.warning(
            "create_evaluation: invalid field_owner %s", field_owner
        )
        return None
    if verdict not in VALID_VERDICTS:
        logger.warning(
            "create_evaluation: invalid verdict %s", verdict
        )
        return None
    if criticality not in VALID_CRITICALITIES:
        criticality = "normal"
    if not field_name or not reasoning:
        return None

    pool = get_pool()
    if pool is None:
        return None

    eval_id = str(uuid.uuid4())
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO atlas_evaluation (
                        eval_id, rec_id, workspace_id,
                        field_name, submitted_value, field_owner,
                        verdict, reasoning, citations,
                        proposed_alternative, test_design, evidence_path,
                        confidence, criticality, meta
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s::jsonb,
                        %s, %s, %s,
                        %s, %s, %s::jsonb
                    )
                    """,
                    (
                        eval_id, rec_id, workspace_id,
                        field_name, submitted_value, field_owner,
                        verdict, reasoning,
                        json.dumps(citations or []),
                        proposed_alternative, test_design, evidence_path,
                        confidence, criticality,
                        json.dumps(meta or {}),
                    ),
                )
            conn.commit()
        return eval_id
    except Exception as exc:
        logger.warning("create_evaluation failed: %s", exc)
        return None


def list_evaluations(
    rec_id: str,
    *,
    pending_only: bool = False,
    field_owner: Optional[str] = None,
) -> list[dict[str, Any]]:
    """All evaluations on a given rec, in eval order."""
    pool = get_pool()
    if pool is None:
        return []
    where = ["rec_id = %s"]
    params: list[Any] = [rec_id]
    if pending_only:
        where.append("operator_decision IS NULL")
    if field_owner:
        where.append("field_owner = %s")
        params.append(field_owner)
    sql = f"""
        SELECT eval_id, rec_id, workspace_id, field_name, submitted_value,
               field_owner, verdict, reasoning, citations,
               proposed_alternative, test_design, evidence_path,
               confidence, criticality,
               agency_response, agency_response_at, agency_confidence,
               operator_decision, operator_decided_at, operator_reasoning,
               final_value, evaluated_at, meta
        FROM atlas_evaluation
        WHERE {' AND '.join(where)}
        ORDER BY evaluated_at
    """
    out: list[dict[str, Any]] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                for r in cur.fetchall():
                    out.append(_row_to_dict(r))
    except Exception as exc:
        logger.warning("list_evaluations failed: %s", exc)
    return out


def get_evaluation(eval_id: str) -> Optional[dict[str, Any]]:
    """Fetch one evaluation by id."""
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT eval_id, rec_id, workspace_id, field_name,
                           submitted_value, field_owner, verdict, reasoning,
                           citations, proposed_alternative, test_design,
                           evidence_path, confidence, criticality,
                           agency_response, agency_response_at,
                           agency_confidence, operator_decision,
                           operator_decided_at, operator_reasoning,
                           final_value, evaluated_at, meta
                    FROM atlas_evaluation
                    WHERE eval_id = %s
                    """,
                    (eval_id,),
                )
                r = cur.fetchone()
                if not r:
                    return None
                return _row_to_dict(r)
    except Exception as exc:
        logger.warning("get_evaluation failed: %s", exc)
        return None


def _row_to_dict(r: tuple) -> dict[str, Any]:
    return {
        "eval_id": r[0],
        "rec_id": r[1],
        "workspace_id": r[2],
        "field_name": r[3],
        "submitted_value": r[4],
        "field_owner": r[5],
        "verdict": r[6],
        "reasoning": r[7],
        "citations": r[8] or [],
        "proposed_alternative": r[9],
        "test_design": r[10],
        "evidence_path": r[11],
        "confidence": float(r[12]) if r[12] is not None else None,
        "criticality": r[13],
        "agency_response": r[14],
        "agency_response_at":
            r[15].isoformat() if r[15] else None,
        "agency_confidence": r[16],
        "operator_decision": r[17],
        "operator_decided_at":
            r[18].isoformat() if r[18] else None,
        "operator_reasoning": r[19],
        "final_value": r[20],
        "evaluated_at": r[21].isoformat() if r[21] else None,
        "meta": r[22] or {},
    }


def apply_agency_response(
    eval_id: str,
    *,
    response_text: str,
    agency_confidence: Optional[int] = None,
) -> bool:
    """Write the agency's response (from tokenized link) onto an eval row."""
    if not response_text:
        return False
    pool = get_pool()
    if pool is None:
        return False
    if agency_confidence is not None:
        agency_confidence = max(1, min(5, int(agency_confidence)))
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE atlas_evaluation
                    SET agency_response = %s,
                        agency_response_at = NOW(),
                        agency_confidence = %s
                    WHERE eval_id = %s
                    """,
                    (response_text.strip(), agency_confidence, eval_id),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("apply_agency_response failed: %s", exc)
        return False


def apply_operator_decision(
    eval_id: str,
    *,
    decision: str,
    final_value: Optional[str] = None,
    reasoning: Optional[str] = None,
) -> bool:
    """Record the operator's final call. Sets final_value when present."""
    if decision not in VALID_OPERATOR_DECISIONS:
        logger.warning(
            "apply_operator_decision: invalid decision %s", decision
        )
        return False
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE atlas_evaluation
                    SET operator_decision = %s,
                        operator_decided_at = NOW(),
                        operator_reasoning = %s,
                        final_value = %s
                    WHERE eval_id = %s
                    """,
                    (decision, reasoning, final_value, eval_id),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("apply_operator_decision failed: %s", exc)
        return False


def summarize_rec(rec_id: str) -> dict[str, Any]:
    """Counts by verdict, by field_owner, and pending-operator-decision."""
    pool = get_pool()
    if pool is None:
        return {}
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      COUNT(*) FILTER (WHERE verdict = 'agree'),
                      COUNT(*) FILTER (WHERE verdict = 'partial'),
                      COUNT(*) FILTER (WHERE verdict = 'disagree'),
                      COUNT(*) FILTER (WHERE verdict = 'unknown'),
                      COUNT(*) FILTER (WHERE operator_decision IS NULL),
                      COUNT(*) FILTER (
                          WHERE field_owner = 'agency'
                            AND agency_response IS NULL
                      ),
                      COUNT(*) FILTER (
                          WHERE field_owner = 'manufacturer'
                      ),
                      COUNT(*)
                    FROM atlas_evaluation
                    WHERE rec_id = %s
                    """,
                    (rec_id,),
                )
                r = cur.fetchone()
                if not r:
                    return {}
                return {
                    "agree": r[0] or 0,
                    "partial": r[1] or 0,
                    "disagree": r[2] or 0,
                    "unknown": r[3] or 0,
                    "pending_operator_decision": r[4] or 0,
                    "awaiting_agency_response": r[5] or 0,
                    "manufacturer_fields": r[6] or 0,
                    "total": r[7] or 0,
                }
    except Exception as exc:
        logger.warning("summarize_rec failed: %s", exc)
        return {}


__all__ = [
    "create_evaluation",
    "list_evaluations",
    "get_evaluation",
    "apply_agency_response",
    "apply_operator_decision",
    "summarize_rec",
    "VALID_FIELD_OWNERS",
    "VALID_VERDICTS",
    "VALID_CRITICALITIES",
    "VALID_OPERATOR_DECISIONS",
]
