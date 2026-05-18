"""Atlas substrate — operator_positions.

Implements OPERATOR_POSITIONS.md. The operator's beliefs and rules as
substrate. Read by build_context() in Layer 2; written by the
promotion-from-edit flow.

Contract:
    create_position(...)        -> str | None
    archive_position(...)       -> bool
    supersede_position(...)     -> bool
    list_active_positions(...)  -> list[dict]
    read_active_positions(...)  -> list[dict]   (scope-prioritised read)

Best-effort writes. Never raises.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.operator_positions")


VALID_SCOPES = (
    "global", "brand", "asin", "family",
    "decision_class", "family_decision_class",
)

VALID_POSITION_TYPES = (
    "strategic", "style", "hard_refusal", "workflow", "preference",
)

VALID_STATUSES = ("active", "archived", "superseded")


# Scope specificity for read-priority (lower = less specific).
SCOPE_SPECIFICITY = {
    "global": 0,
    "brand": 1,
    "decision_class": 2,
    "family": 3,
    "family_decision_class": 4,
    "asin": 5,
}


def create_position(
    workspace_id: str,
    *,
    scope: str,
    scope_ref: Optional[str],
    claim: str,
    reasoning: Optional[str] = None,
    position_type: str = "strategic",
    operator_id: str = "devang",
    evidence_refs: Optional[list[str]] = None,
    created_by_event_id: Optional[str] = None,
) -> Optional[str]:
    """Create a new active operator_position. Returns position_id."""
    if scope not in VALID_SCOPES:
        logger.warning("create_position: invalid scope %s", scope)
        return None
    if position_type not in VALID_POSITION_TYPES:
        logger.warning(
            "create_position: invalid position_type %s", position_type
        )
        return None
    if not claim or not claim.strip():
        return None

    pool = get_pool()
    if pool is None:
        return None

    pid = str(uuid.uuid4())
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO operator_positions (
                        position_id, workspace_id, operator_id,
                        scope, scope_ref, claim, reasoning,
                        position_type, status, evidence_refs,
                        revision, created_by_event_id
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, 'active', %s,
                        1, %s
                    )
                    """,
                    (
                        pid, workspace_id, operator_id,
                        scope, scope_ref, claim.strip(), reasoning,
                        position_type, evidence_refs or [],
                        created_by_event_id,
                    ),
                )
            conn.commit()
        return pid
    except Exception as exc:
        logger.warning("create_position failed: %s", exc)
        return None


def archive_position(position_id: str, archived_by: str) -> bool:
    """Mark a position archived. Does not delete."""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE operator_positions
                    SET status = 'archived',
                        meta = COALESCE(meta, '{}'::jsonb)
                               || jsonb_build_object(
                                    'archived_by', %s::text,
                                    'archived_at', to_jsonb(NOW())
                                  )
                    WHERE position_id = %s AND status = 'active'
                    """,
                    (archived_by, position_id),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("archive_position failed: %s", exc)
        return False


def supersede_position(
    old_position_id: str,
    new_position_id: str,
    superseded_by_operator: str,
) -> bool:
    """Mark an old position as superseded by a newer one."""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE operator_positions
                    SET status = 'superseded',
                        superseded_by = %s,
                        meta = COALESCE(meta, '{}'::jsonb)
                               || jsonb_build_object(
                                    'superseded_by_operator', %s::text,
                                    'superseded_at', to_jsonb(NOW())
                                  )
                    WHERE position_id = %s AND status = 'active'
                    """,
                    (new_position_id, superseded_by_operator, old_position_id),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("supersede_position failed: %s", exc)
        return False


def list_active_positions(
    workspace_id: str,
    *,
    scope: Optional[str] = None,
    scope_ref: Optional[str] = None,
    position_type: Optional[str] = None,
) -> list[dict[str, Any]]:
    """All active positions matching optional filters."""
    pool = get_pool()
    if pool is None:
        return []

    where = ["workspace_id = %s", "status = 'active'"]
    params: list[Any] = [workspace_id]
    if scope:
        where.append("scope = %s")
        params.append(scope)
    if scope_ref:
        where.append("scope_ref = %s")
        params.append(scope_ref)
    if position_type:
        where.append("position_type = %s")
        params.append(position_type)

    sql = f"""
        SELECT position_id, scope, scope_ref, claim, reasoning,
               position_type, evidence_refs, revision,
               created_at, created_by_event_id, last_reviewed_at, meta
        FROM operator_positions
        WHERE {' AND '.join(where)}
        ORDER BY created_at DESC
    """
    out = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                for r in cur.fetchall():
                    out.append({
                        "position_id": r[0],
                        "scope": r[1],
                        "scope_ref": r[2],
                        "claim": r[3],
                        "reasoning": r[4],
                        "position_type": r[5],
                        "evidence_refs": r[6] or [],
                        "revision": r[7],
                        "created_at": r[8].isoformat() if r[8] else None,
                        "created_by_event_id": r[9],
                        "last_reviewed_at":
                            r[10].isoformat() if r[10] else None,
                        "meta": r[11] or {},
                    })
    except Exception as exc:
        logger.warning("list_active_positions failed: %s", exc)
    return out


def read_active_positions(
    workspace_id: str,
    *,
    asin: Optional[str] = None,
    family: Optional[str] = None,
    decision_class: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Read positions in scope-priority order (specificity-first).

    Matching rules per OPERATOR_POSITIONS.md §Read path:
      - hard_refusal at any matching level always applies
      - scope=asin matching this ASIN
      - scope=family matching this family
      - scope=decision_class matching this decision_class
      - scope=family_decision_class compound matches
      - scope=brand
      - scope=global

    Returns positions sorted by specificity DESC then created_at DESC.
    Equal-specificity conflicts: most recent wins (a flag is logged).
    """
    pool = get_pool()
    if pool is None:
        return []

    # We pull every active row matching one of the scopes we care about,
    # then sort in Python so specificity logic stays explicit.
    fdc_ref = (
        f"{family}|{decision_class}"
        if family and decision_class else None
    )

    params: list[Any] = [workspace_id]
    or_parts = ["scope = 'global'", "scope = 'brand'"]
    if asin:
        or_parts.append("(scope = 'asin' AND scope_ref = %s)")
        params.append(asin)
    if family:
        or_parts.append("(scope = 'family' AND scope_ref = %s)")
        params.append(family)
    if decision_class:
        or_parts.append("(scope = 'decision_class' AND scope_ref = %s)")
        params.append(decision_class)
    if fdc_ref:
        or_parts.append("(scope = 'family_decision_class' AND scope_ref = %s)")
        params.append(fdc_ref)

    or_clause = " OR ".join(or_parts)

    sql = f"""
        SELECT position_id, scope, scope_ref, claim, reasoning,
               position_type, evidence_refs, revision,
               created_at, created_by_event_id, meta
        FROM operator_positions
        WHERE workspace_id = %s
          AND status = 'active'
          AND ({or_clause})
        ORDER BY created_at DESC
    """

    rows: list[dict[str, Any]] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                for r in cur.fetchall():
                    rows.append({
                        "position_id": r[0],
                        "scope": r[1],
                        "scope_ref": r[2],
                        "claim": r[3],
                        "reasoning": r[4],
                        "position_type": r[5],
                        "evidence_refs": r[6] or [],
                        "revision": r[7],
                        "created_at": r[8].isoformat() if r[8] else None,
                        "created_by_event_id": r[9],
                        "meta": r[10] or {},
                        "_specificity":
                            SCOPE_SPECIFICITY.get(r[1] or "", 0),
                    })
    except Exception as exc:
        logger.warning("read_active_positions failed: %s", exc)
        return []

    # Sort specificity DESC then created_at DESC. Hard refusals at any level
    # bubble first by tagging them with a synthetic +100 specificity weight.
    def _sort_key(row: dict[str, Any]) -> tuple[int, str]:
        spec = row["_specificity"]
        if row["position_type"] == "hard_refusal":
            spec += 100
        return (-spec, row["created_at"] or "")

    rows.sort(key=_sort_key)

    # Flag conflicts at equal specificity (same scope, same scope_ref,
    # different active positions). Operator review prompt.
    seen: dict[tuple[str, Optional[str]], str] = {}
    for r in rows:
        key = (r["scope"], r["scope_ref"])
        if key in seen and seen[key] != r["position_id"]:
            logger.info(
                "Position conflict at scope=%s ref=%s: multiple active",
                key[0], key[1],
            )
        else:
            seen[key] = r["position_id"]

    return rows


def get_position(position_id: str) -> Optional[dict[str, Any]]:
    """Fetch one position by id."""
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT position_id, workspace_id, operator_id, scope,
                           scope_ref, claim, reasoning, position_type,
                           status, superseded_by, evidence_refs, revision,
                           created_at, last_reviewed_at, meta
                    FROM operator_positions
                    WHERE position_id = %s
                    """,
                    (position_id,),
                )
                r = cur.fetchone()
                if not r:
                    return None
                return {
                    "position_id": r[0],
                    "workspace_id": r[1],
                    "operator_id": r[2],
                    "scope": r[3],
                    "scope_ref": r[4],
                    "claim": r[5],
                    "reasoning": r[6],
                    "position_type": r[7],
                    "status": r[8],
                    "superseded_by": r[9],
                    "evidence_refs": r[10] or [],
                    "revision": r[11],
                    "created_at": r[12].isoformat() if r[12] else None,
                    "last_reviewed_at":
                        r[13].isoformat() if r[13] else None,
                    "meta": r[14] or {},
                }
    except Exception as exc:
        logger.warning("get_position failed: %s", exc)
        return None


__all__ = [
    "create_position",
    "archive_position",
    "supersede_position",
    "list_active_positions",
    "read_active_positions",
    "get_position",
    "VALID_SCOPES",
    "VALID_POSITION_TYPES",
    "VALID_STATUSES",
]
