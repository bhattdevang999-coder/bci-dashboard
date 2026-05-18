"""Atlas substrate — unknowns catalog.

Implementation of UNKNOWNS.md. The dashboard's first job is to know
what it doesn't know. Every reasoning chain that hits a missing input
emits an unknowns row; operator-actionable; routed to the right owner
queue; closeable as evidence arrives.

Contract:
    emit_unknown(...)        -> str (unknown_id)
    resolve_unknown(...)     -> bool
    list_open_unknowns(...)  -> list[dict]
    declare_unknowable(...)  -> bool

Never raises. Best-effort writes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.unknowns")


# Evidence paths route to operator queues.
VALID_EVIDENCE_PATHS = (
    "factory_spec_sheet",
    "agency_response",
    "helium10_weekly",
    "jungle_scout_weekly",
    "keepa_weekly",
    "a_b_test",
    "outcome_measurement",
    "operator_decision",
    "declared_unknowable",
)

VALID_SCOPES = ("global", "brand", "asin", "family", "decision_class")

VALID_PRIORITIES = ("launch_blocking", "high", "normal", "low")

VALID_STATUSES = (
    "open", "partial", "answered", "declared_unknowable", "expired",
)


def _dedupe_key(workspace_id: str, scope: str, scope_ref: Optional[str],
                question: str, evidence_path: str) -> str:
    """Stable hash for deduplication on (ws, scope, scope_ref, question,
    evidence_path). Two emits with same key append rather than duplicate."""
    raw = f"{workspace_id}|{scope}|{scope_ref or ''}|{question.strip().lower()}|{evidence_path}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def emit_unknown(
    workspace_id: str,
    scope: str,
    scope_ref: Optional[str],
    question: str,
    required_for: list[str],
    evidence_path: str,
    *,
    priority: str = "normal",
    created_by_event_id: Optional[str] = None,
    created_by_module: Optional[str] = None,
) -> Optional[str]:
    """Emit an unknown into the catalog.

    Dedupe: same (workspace_id, scope, scope_ref, question, evidence_path)
    appends required_for to existing row instead of creating a duplicate.

    Returns unknown_id on success, None on failure.
    """
    if scope not in VALID_SCOPES:
        logger.warning("emit_unknown: invalid scope %s", scope)
        return None
    if evidence_path not in VALID_EVIDENCE_PATHS:
        logger.warning("emit_unknown: invalid evidence_path %s", evidence_path)
        return None
    if priority not in VALID_PRIORITIES:
        priority = "normal"

    pool = get_pool()
    if pool is None:
        return None

    dedupe = _dedupe_key(workspace_id, scope, scope_ref, question, evidence_path)
    meta = {"dedupe_key": dedupe}

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # Check for existing open/partial unknown with same dedupe key
                cur.execute(
                    """
                    SELECT unknown_id, required_for
                    FROM unknowns
                    WHERE workspace_id = %s
                      AND meta->>'dedupe_key' = %s
                      AND status IN ('open', 'partial')
                    LIMIT 1
                    """,
                    (workspace_id, dedupe),
                )
                row = cur.fetchone()
                if row is not None:
                    existing_id, existing_for = row
                    merged = list(set((existing_for or []) + (required_for or [])))
                    cur.execute(
                        """
                        UPDATE unknowns
                        SET required_for = %s,
                            priority = CASE
                                WHEN %s = 'launch_blocking' THEN 'launch_blocking'
                                WHEN priority = 'launch_blocking' THEN 'launch_blocking'
                                WHEN %s = 'high' OR priority = 'high' THEN 'high'
                                ELSE priority
                            END
                        WHERE unknown_id = %s
                        """,
                        (merged, priority, priority, existing_id),
                    )
                    conn.commit()
                    return existing_id

                # Insert new
                uid = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO unknowns (
                        unknown_id, workspace_id, scope, scope_ref,
                        question, required_for, evidence_path, status,
                        priority, created_by_event_id, created_by_module,
                        meta
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, 'open',
                        %s, %s, %s, %s::jsonb
                    )
                    """,
                    (
                        uid, workspace_id, scope, scope_ref,
                        question, required_for, evidence_path,
                        priority, created_by_event_id, created_by_module,
                        json.dumps(meta),
                    ),
                )
            conn.commit()
            return uid
    except Exception as exc:
        logger.warning("emit_unknown write failed: %s", exc)
        return None


def list_open_unknowns(
    workspace_id: str,
    *,
    scope: Optional[str] = None,
    scope_ref: Optional[str] = None,
    evidence_path: Optional[str] = None,
    decision_class: Optional[str] = None,
    statuses: tuple[str, ...] = ("open", "partial"),
) -> list[dict[str, Any]]:
    """List unknowns matching filters. Used by Layer-0 context builder
    + the operator-facing Unknowns dashboard."""
    pool = get_pool()
    if pool is None:
        return []

    where = ["workspace_id = %s", "status = ANY(%s)"]
    params: list[Any] = [workspace_id, list(statuses)]
    if scope:
        where.append("scope = %s")
        params.append(scope)
    if scope_ref:
        where.append("scope_ref = %s")
        params.append(scope_ref)
    if evidence_path:
        where.append("evidence_path = %s")
        params.append(evidence_path)
    if decision_class:
        where.append("%s = ANY(required_for)")
        params.append(decision_class)

    sql = f"""
        SELECT unknown_id, scope, scope_ref, question, required_for,
               evidence_path, status, priority, created_at,
               created_by_event_id, created_by_module, partial_evidence
        FROM unknowns
        WHERE {' AND '.join(where)}
        ORDER BY
          CASE priority
            WHEN 'launch_blocking' THEN 0
            WHEN 'high' THEN 1
            WHEN 'normal' THEN 2
            WHEN 'low' THEN 3
            ELSE 4
          END,
          created_at DESC
    """

    rows: list[dict[str, Any]] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                for r in cur.fetchall():
                    rows.append({
                        "unknown_id": r[0],
                        "scope": r[1],
                        "scope_ref": r[2],
                        "question": r[3],
                        "required_for": r[4] or [],
                        "evidence_path": r[5],
                        "status": r[6],
                        "priority": r[7],
                        "created_at": r[8].isoformat() if r[8] else None,
                        "created_by_event_id": r[9],
                        "created_by_module": r[10],
                        "partial_evidence": r[11] or [],
                    })
    except Exception as exc:
        logger.warning("list_open_unknowns failed: %s", exc)
    return rows


def resolve_unknown(
    unknown_id: str,
    answer_value: Any,
    answer_source: str,
    answered_by: str,
    *,
    status: str = "answered",
) -> bool:
    """Mark an unknown as answered or declared_unknowable.

    Note: this does NOT propagate the answer to canonical substrate
    tables (e.g., asin_metadata). That propagation is the caller's
    responsibility per UNKNOWNS.md §"When unknowns close" — keeps this
    function simple and avoids accidental writes to other tables.
    """
    if status not in ("answered", "declared_unknowable", "partial"):
        return False
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE unknowns
                    SET status = %s,
                        answer_value = %s::jsonb,
                        answer_source = %s,
                        answered_at = NOW(),
                        answered_by = %s
                    WHERE unknown_id = %s
                    """,
                    (
                        status,
                        json.dumps(answer_value) if answer_value is not None else None,
                        answer_source,
                        answered_by,
                        unknown_id,
                    ),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("resolve_unknown failed: %s", exc)
        return False


def declare_unknowable(
    unknown_id: str,
    reasoning: str,
    declared_by: str,
) -> bool:
    """Operator explicitly declares this gap irreducible. Removes it
    from confidence calculations going forward."""
    return resolve_unknown(
        unknown_id,
        {"declared_unknowable": True, "reasoning": reasoning},
        "operator_declaration",
        declared_by,
        status="declared_unknowable",
    )


__all__ = [
    "emit_unknown",
    "list_open_unknowns",
    "resolve_unknown",
    "declare_unknowable",
    "VALID_EVIDENCE_PATHS",
    "VALID_SCOPES",
    "VALID_PRIORITIES",
    "VALID_STATUSES",
]
